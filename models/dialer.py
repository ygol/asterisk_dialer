# -*- coding: utf-8 -*-
import ari
import datetime
import logging
import time
import threading
import os, sys, traceback
import uuid
from openerp import fields, models, api, sql_db, _
from openerp.exceptions import ValidationError, DeferredException, MissingError
from requests.exceptions import HTTPError, ConnectionError

_logger = logging.getLogger(__name__)

DIALER_RUN_SLEEP = 10 # Dialer thread sleep seconds 




class AriOdooSessionThread(threading.Thread):

    def init_ari(self, ari_url, ari_user, ari_pass):
        self.ari_url = ari_url
        self.ari_user = ari_user
        self.ari_pass = ari_pass
        self.ari_client = ari.connect(ari_url, ari_user, ari_pass)
        
        
    def init_env(self, dbname, uid, session_id):
        self.cursor = sql_db.db_connect(dbname).cursor()
        self.env = api.Environment(self.cursor, uid, {})
        self.env.cr.autocommit(True)
        self.session = self.env['asterisk.dialer.session'].browse([session_id])
        self.dialer = self.session.dialer


class StasisThread(AriOdooSessionThread):
    """
    Terminate conditions:
    1) No more calls to originate and no more channels are expected to come into Stasis
    2) Origination thread exits due to Pause / Cancel
    Ignore errors:
    1) On answer / playback / hangup events
    """

    def run(self):
        
        def stasis_start(channel, ev):
        
            def answer_channel(channel):
            
                def playback_finished(playback, event):
                            
                    def hangup():
                        try:
                            channel.hangup()
                        except:
                            # Channel was already hangup by user side
                            pass
                    
                    t = threading.Timer(1, hangup)
                    t.start()
                            
                try:
                    channel.answer()
                    playback_id = str(uuid.uuid4())
                    sound_file = os.path.splitext(self.dialer.sound_file.get_full_path()[0])[0]
                    playback = channel.play(channel_id=channel.id, media='sound:%s' % sound_file)
                    playback.on_event('PlaybackFinished', playback_finished)
                except:
                    # Channel was already hangup by user side.
                    raise # TODO: Set exception type. 
                    return
                
        # Stasis start
        with api.Environment.manage():
            
            t = threading.Timer(1, answer_channel, [channel])
            t.start()

            try:
                client = ari.connect(self.ari_url, self.ari_user, self.ari_pass)
                client.on_channel_event('StasisStart', stasis_start)
                client.on_channel_event('ChannelUserevent', user_event)
                client.run(apps='dialer-%s-session-%s' % (self.dialer.id, self.session.id))

            except:
                raise
            
            finally:
                self.cursor.close()


class OriginationThread(AriOdooSessionThread):

    go_next_call = threading.Event()
    
    def create_channel(self, contact):
        """
        Create new active channel.
        """
        chan_id = uuid.uuid1()
        timestamp = int(time.time())
        channel_id = '%s-%s-1' % (chan_id, timestamp)
        otherchannel_id = '%s-%s-2' % (chan_id, timestamp)

        channel = self.env['asterisk.dialer.channel'].create({
            'dialer': self.dialer.id,
            'session': self.session.id,
            'channel_id': channel_id,
            'other_channel_id': otherchannel_id,
            'phone': contact.phone,
            'start_time': datetime.datetime.now(),
            'name': contact.name})
        _logger.debug('CHANNELS CREATED: %s, %s.' % (channel_id, otherchannel_id))
        return channel_id, otherchannel_id


    def update_cdr(self, contact, channel_id, otherchannel_id):
        """
        Update a CDR created before on session create.
        """
        cdr = self.env['asterisk.dialer.cdr'].search([('id','=', contact.id)])
        if cdr:
            cdr.write({
                    'channel_id': channel_id,
                    'other_channel_id': otherchannel_id,
                    'status': 'process',
                    'start_time': datetime.datetime.now(),
                    })
            _logger.debug('CDR UPDATED: %s, %s, %s.' %(contact.phone, channel_id, otherchannel_id))
        else:
            _logger.warn('CDR not found! Phone: %s.' % contact.phone)


    def originate_call(self, contact):
        """
        ARI call origination method.
        """
        channel_id, otherchannel_id = self.create_channel(contact)
        self.update_cdr(contact, channel_id, otherchannel_id)
        
        if self.dialer.dialer_type == 'playback':
            ari_channel = self.ari_client.channels.originate(
                        endpoint='Local/%s@dialer' % (contact.phone),
                        app='dialer-%s-session-%s' % (self.dialer.id, self.session.id),                        
                        channelId=channel_id,
                        otherChannelId=otherchannel_id)
        else:
            ari_channel = self.ari_client.channels.originate(
                        endpoint='Local/%s@dialer' % (contact.phone),                        
                        context='%s' % self.dialer.context_name, extension='%s' % contact.phone, priority='1',
                        channelId=channel_id,
                        otherChannelId=otherchannel_id)

        # Increment sent counter
        self.session.sent += 1
        _logger.debug('CALL ORIGINATED: %s' % contact.phone)


    def get_channel_count(self):
        channel_count = None
        while channel_count == None:
            try:
                self.env['asterisk.dialer.channel'].invalidate_cache()
                channel_count = self.dialer.channel_count
            except MissingError:
                # In rare cases we get a racing condition here.
                time.sleep(0.1)
        _logger.debug('CHANNEL COUNT: %s.' % channel_count)
        return channel_count


    def request_stasis_exit(self):
        if self.dialer.dialer_type != 'playback':
            # Stasis app was not started
            return
        try:
            # Method 1: Send exit_request via ARI
            self.ari_client.events.userEvent(eventName='exit_request',
                                            application ='dialer-%s-session-%s' % (
                                                            self.dialer.id, self.session.id))
            # Method 2: Set event (in case ARI will be down there)
            # TODO:
            _logger.debug('STASIS EXIT REQUESTED.')
        except HTTPError:
            # Stasis app was already terminated
            pass
        
        
    def run(self):
        """
        Main thread loop. Condition to terminate:
        1) All done. Also terminate Stasis app.
        2) Stasis app not ready (when dialer type is playback)
        3) ARI connection error. Also terminate Stasis app.
        4) Pause / cancel requested. Also terminate Stasis app.
        """
        with api.Environment.manage():
        
            try:
                cdrs = self.env['asterisk.dialer.cdr'].search(
                                            [('session','=',self.session.id),
                                            ('status','=','queue')])
                _logger.debug('CDRS LEN: %s' % len(cdrs))
                
                self.cdrs = iter(cdrs)
            
                while True:
                    self.dialer.invalidate_cache()
                    # Reset flag on every round
                    self.go_next_call.clear()
                    # Check for cancel request
                    if self.dialer.cancel_request:
                        self.session.state = 'cancelled'
                        self.session.cancel_request = False                    
                        self.request_stasis_exit()
                        _logger.debug('DIALER: CANCEL REQUEST')
                        return

                    # Check for pause request
                    elif self.dialer.pause_request:
                        self.session.state = 'paused'
                        self.session.pause_request = False
                        self.request_stasis_exit()
                        _logger.debug('DIALER: PAUSE REQUEST')
                        return

                    # Check if we can add more calls 
                    if self.get_channel_count() < self.dialer.simult:
                        # We can add channels, just go on!
                        cdr = self.cdrs.next()
                        self.originate_call(cdr)
                    else:                        
                        _logger.debug('NO CHANNELS AVAILABLE, SLEEPING.')
                        # Sleep here or be interrupted by hangup 
                        self.go_next_call.wait(DIALER_RUN_SLEEP)


            except StopIteration:
                self.session.state = 'done'
                self.request_stasis_exit()
                _logger.debug('CDR StopIteration.')
            

            except ConnectionError, e:
                # ARI Error
                self.request_stasis_exit()
                self.session.state = 'error'
                self.dialer.message_post('ARI ConnectionError: %s' % e.message)
                _logger.debug('ARI CONNECTION ERROR.')
                
            finally:            
                try:                
                    self.cursor.close()
                    self.ari_client.close()
                    _logger.debug('ORIGINATE FINALLY CLOSING.')
                except:
                    pass
        





DIALER_TYPE_CHOICES = (    
    ('playback', _('Playback message')),
    ('dialplan', _('Asterisk dialplan')),
)


class dialer(models.Model):
    _name = 'asterisk.dialer'
    _inherit = 'mail.thread'
    _description = 'Asterisk Dialer'
    _order = 'name'


    @api.one
    def _get_cdr_count(self):
        self.cdr_count = self.env['asterisk.dialer.cdr'].search_count([('dialer','=',self.id)])
    
    
    @api.one
    def _get_state(self):
        session = self.env['asterisk.dialer.session'].search([('dialer','=',self.id)], order='create_date desc', limit=1)
        if session.state == 'done':
            self.state = "<span class='glyphicon glyphicon-ok'/>"
        elif session.state == 'cancelled':
                self.state = "<span class='glyphicon glyphicon-eject'/>"
        elif session.state == 'paused':
            self.state = "<span class='glyphicon glyphicon-time'/>"
        elif session.state == 'running':
            self.state = "<span class='glyphicon glyphicon-bullhorn'/>"
        elif session.state == 'error':
            self.state = "<span class='glyphicon glyphicon-exclamation-sign'/>"
        else:
            self.state = "<span class='glyphicon glyphicon-edit'/>"
        
        
    @api.one
    def _get_active_session(self):
        """Get latest session"""
        session = self.env['asterisk.dialer.session'].search([
            ('dialer','=', self.id)],
            order='create_date desc', limit=1)
        self.active_session = session
        
    @api.one
    @api.depends('channels')
    def _get_channel_count(self):
        self.channel_count = len(self.channels)
        

    name = fields.Char(required=True, string=_('Name'))
    description = fields.Text(string=_('Description'))
    dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'),
                                    default='playback')
    context_name = fields.Char(string=_('Context name'), default='')
    state = fields.Html(compute='_get_state', string=_('State'), default='draft')
    active_session = fields.Many2one('asterisk.dialer.session', compute='_get_active_session')
    active_session_state = fields.Selection(related='active_session.state')
    sessions = fields.One2many('asterisk.dialer.session', 'dialer')
    sound_file = fields.Many2one('asterisk.dialer.soundfile', string=_('Sound file'), 
        ondelete='restrict')
    #start_time = fields.Datetime(string=_('Start time'), 
    #    help=_('Exact date and time to start dialing. For scheduled dialers.'))
    from_time = fields.Float(digits=(2, 2), string=_('From time'), default=10.00,
        help=_('Time permitted for calling If dialer is paused it will be resumed this time.')) 
    to_time = fields.Float(digits=(2, 2), string=_('To time'), default=18.00,
        help=_('Time perimitted for calling. If dialer is running it will be paused this time')) 
    contacts = fields.Many2many(comodel_name='asterisk.dialer.contacts',
                                relation='asterisk_dialer_contacts_rel')
    channels = fields.One2many('asterisk.dialer.channel', 'dialer', string=_('Current calls'))
    channel_count = fields.Integer(compute='_get_channel_count')
    cdrs = fields.One2many('asterisk.dialer.cdr', 'dialer', string=_('Call Detail Records'))
    cdr_count = fields.Integer(compute='_get_cdr_count', string=_('Number of call detail records'))
    simult = fields.Integer(string=_('Simultaneous calls'), default=1)
    cancel_request = fields.Boolean(related='active_session.cancel_request')
    pause_request = fields.Boolean(related='active_session.pause_request')
  
  
    
    @api.one
    def start(self):        
        # Validations before start
        if not self.contacts:
            raise ValidationError(_('You have nobody to dial. Add contacts first :-)'))
        elif self.dialer_type == 'playback' and not self.sound_file:
            raise ValidationError(_('Dialer type is Playback and sound file not set!'))
        elif self.dialer_type == 'dialplan' and not self.context_name:
            raise ValidationError(_('Dialer type is Dialplan and Asterisk context not set!'))

        # Get / create active session
        session = self.env['asterisk.dialer.session'].search([
                                ('dialer','=', self.id),
                                ('state', 'in', ['error', 'paused'])],
                                order='create_date desc', limit=1)
        
        if not session:
            _logger.debug('NO INTERRUPTED SESSION, CREATING ONE.')
            self.env.cr.autocommit(False)
            session = self.env['asterisk.dialer.session'].create(
                                                        {'dialer': self.id})
            total_count = 0
            for group in self.contacts:
                for contact in self.env[group.model].search(eval(group.model_domain)):
                    self.env['asterisk.dialer.cdr'].create({
                        'phone': contact.phone,
                        'name': contact.name,
                        'dialer': self.id,
                        'session': session.id,
                        'status': 'queue',
                    })
                    total_count += 1
            session.total = total_count
            self.env.cr.commit()
            self.env.cr.autocommit(True)        
        
        session.state = 'running'
        # Reset channels
        self.channels.unlink()

        # ARI configuration
        server = self.env['asterisk.server.settings'].browse([1])
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str('http://' + server.ip_addr + ':' + server.http_port)

        # Init threads
        stasis_thread = None
        try:
            if self.dialer_type == 'playback':
                stasis_thread = StasisThread()
                stasis_thread.setName('StasisThread-%s' % self.id) # Name this thread according to dialer id
                stasis_thread.init_ari(ari_url, ari_user, ari_pass)
                stasis_thread.init_env(self.env.cr.dbname, self.env.uid, session.id)

            origination_thread = OriginationThread()
            origination_thread.setName('OriginationThread-%s' % self.id)
            origination_thread.init_ari(ari_url, ari_user, ari_pass)
            origination_thread.init_env(self.env.cr.dbname, self.env.uid, session.id)

            if stasis_thread:
                origination_thread.stasis_thread = stasis_thread
                stasis_thread.origination_thread = origination_thread

        except ConnectionError:
            del stasis_thread
            del origination_thread
            raise ValidationError(_('Cannot connect to %s using login: %s and password: %s. Check that Asterisk is running and ARI settings are valid.') %
                (ari_url, ari_user, ari_pass))
        
        # Start threads
        if stasis_thread:
            stasis_thread.start()
        origination_thread.start()

    
        
    @api.one
    def start2(self):
        dialer_id = self.id
        # Validations before start
        if not self.contacts:
            raise ValidationError(_('You have nobody to dial. Add contacts first :-)'))
        elif self.dialer_type == 'playback' and not self.sound_file:
            raise ValidationError(_('Dialer type is Playback and sound file not set!'))
        elif self.dialer_type == 'dialplan' and not self.context_name:
            raise ValidationError(_('Dialer type is Dialplan and Asterisk context not set!'))
        
        # ARI configuration
        server = self.env['asterisk.server.settings'].browse([1])
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str('http://' + server.ip_addr + ':' + server.http_port)
        
        # Get / create active session
        session = self.env['asterisk.dialer.session'].search([
                                ('dialer','=', self.id),
                                ('state', 'in', ['running', 'paused'])],
                                order='create_date desc', limit=1)
        if not session:
            _logger.debug('NO PAUSED SESSION, CREATING ONE.')
            self.env.cr.autocommit(False)
            session = self.env['asterisk.dialer.session'].create(
                                                        {'dialer': self.id})
            total_count = 0
            for group in self.contacts:
                for contact in self.env[group.model].search(eval(group.model_domain)):
                    self.env['asterisk.dialer.cdr'].create({
                        'phone': contact.phone,
                        'name': contact.name,
                        'dialer': self.id,
                        'session': session.id,
                        'status': 'queue',
                    })
                    total_count += 1
            session.total = total_count
            self.env.cr.commit()
            self.env.cr.autocommit(True)
        
        # Reset channels on every start
        self.channels.unlink()
        
        stasis_app_ready = threading.Event()
        go_next_call = threading.Event()
        
                
        def run_stasis_app():
            exit_requested = False
            # Define new cursor for the thread
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()
            new_cr.autocommit(True)
            uid, context = self.env.uid, self.env.context
            with api.Environment.manage():
                self.env = api.Environment(new_cr, uid, context)
                # Re-open dialer with new cursor
                dialer = self.env['asterisk.dialer'].browse([dialer_id])
                
                # Re-open session object with new_cr
                session = self.env['asterisk.dialer.session'].search([
                                ('dialer','=', dialer_id),
                                ('state', 'in', ['running', 'paused'])],
                                order='create_date desc', limit=1)
                
                sound_file = os.path.splitext(self.sound_file.get_full_path()[0])[0]

                try:
                    
                    def exit_on_last_call():
                        channel_count = dialer.env['asterisk.dialer.channel'].search_count(
                                                        [('dialer','=',dialer_id)])
                        _logger.debug('CHANNEL COUNT: %s' % channel_count)
                        print 'CHANNEL COUNT TYPE', type(channel_count), exit_requested
                        if exit_requested and channel_count == 1:
                            # This is last call
                            _logger.debug('No more active channels and exit requested. Exiting.')
                            client.close()
                            return True
                        else:
                            return False
                    
                    def answer_channel(channel):
                        
                        def playback_finished(playback, event):
                            
                            def hangup():
                                try:
                                    channel.hangup()
                                except HTTPError:
                                    pass # User hangup before
                                
                                if not exit_on_last_call():
                                    go_next_call.set() # Originate next call signal to run_dialer
                            
                            t = threading.Timer(1, hangup)
                            t.start()
                            
                            
                        try:
                            channel.answer()
                        except HTTPError:
                            return # 
                        playback_id = str(uuid.uuid4())
                        playback = channel.play(channel_id=channel.id, media='sound:%s' % sound_file)
                        playback.on_event('PlaybackFinished', playback_finished)


                    def stasis_start(channel, ev):
                        t = threading.Timer(1, answer_channel, [channel])
                        t.start()


                    def user_event(channel, ev):                
                        if ev['eventname'] == 'exit_request':
                            exit_requested = True
                            _logger.debug('STASIS: EXIT REQUEST RECEIVED')


                        client = ari.connect(ari_url, ari_user, ari_pass)
                        client.on_channel_event('StasisStart', stasis_start)
                        client.on_channel_event('ChannelUserevent', user_event)
                        stasis_app_ready.set()
                        client.run(apps='dialer-%s-session-%s' % (dialer_id, session.id))
                    
                except Exception, e:
                    # on client.close() we are always here :-) So just ignore it.
                    if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args and e.args[0] == 104: 
                        pass
                    else:
                        # Mark Stasis app as not ok
                        stasis_app_ready.clear()
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        e_txt = '<br/>'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))                     
                        dialer.message_post('Error:\n%s' % e_txt)
                        _logger.debug(e_txt)
                        try:
                            client.close()
                        except:
                            pass

                
                finally:
                    _logger.debug('STASIS APP FINALLY')
                    # If an error happens in Stasis app thread 
                    # let Dialer run thread about it so that it could exit()
                    stasis_app_ready.clear()
                    new_cr.close()
 
 
 

        def run_dialer():
            
            def originate_call(contact):
                _logger.debug('DIALER: ORIGINATE CALL FOR %s' % contact.phone)
                # Generate channel ids
                chan_id = uuid.uuid1()
                channel_id = '%s-1' % chan_id
                otherchannel_id = '%s-2' % chan_id

                # +1 current call
                channel = self.env['asterisk.dialer.channel'].create({
                    'dialer': dialer_id,
                    'session': session.id,
                    'channel_id': channel_id,
                    'other_channel_id': otherchannel_id,
                    'phone': contact.phone,
                    'start_time': datetime.datetime.now(),
                    'name': contact.name})
                                
                # Create CDR
                cdr = self.env['asterisk.dialer.cdr'].search([
                    ('dialer','=', dialer_id),
                    ('session','=', session.id),
                    ('phone','=', contact.phone),
                    ('name','=', contact.name)])
                if cdr:
                    cdr.write({
                    'channel_id': channel_id,
                    'other_channel_id': otherchannel_id,
                    'status': 'process',
                    'start_time': datetime.datetime.now(),
                    })
                else:
                    _logger.warn('CDR not found! Phone: %s' % contact.phone)

                    
                if dialer.dialer_type == 'playback':
                    ari_channel = ari_client.channels.originate(
                        endpoint='Local/%s@dialer' % (contact.phone),
                        app='dialer-%s-session-%s' % (dialer_id, session.id),                        
                        channel_id=channel_id,
                        otherchannel_id=otherchannel_id)
                else:
                    ari_channel = ari_client.channels.originate(
                        endpoint='Local/%s@dialer' % (contact.phone),                        
                        context='%s' % dialer.context_name, extension='%s' % contact.phone, priority='1',
                        channel_id=channel_id,
                        otherchannel_id=otherchannel_id)   
            ## End of originate call

            def exit_stasis_app():
                if dialer.dialer_type == 'playback':
                    try:
                        ari_client.events.userEvent(
                                            eventName='exit_request',
                                            application='dialer-%s-session-%s' % (
                                                            dialer_id, session.id)
                        )
                    except HTTPError:
                        pass
                    
            
            uid, context = self.env.uid, self.env.context
            new_cr = sql_db.db_connect(self.env.cr.dbname).curstor()
            new_cr.autocommit(True)
            
            with api.Environment.manage():                                  
                self.env = api.Environment(new_cr, uid, context)
                
                # Re-open dialer with new cursor
                dialer = self.env['asterisk.dialer'].browse([dialer_id])
                
                # Re-open session object with new_cr
                session = self.env['asterisk.dialer.session'].search([
                                ('dialer','=', dialer_id),
                                ('state', 'in', ['running', 'paused'])],
                                order='create_date desc', limit=1)
                
                ari_client = None   
                try:
                    # Connect to ARI
                    ari_client = ari.connect(ari_url, ari_user, ari_pass)
                    
                    # Main origination loop
                    for cdr in self.env['asterisk.dialer.cdr'].search(
                                                    [('session','=',session.id),
                                                    ('status','=','queue')]):
                        
                        # Clear cash on every round as data could be updated from controller
                        self.env.invalidate_all()
                        
                        
                        def handle_cancel_request():
                            if dialer.cancel_request:
                                session.state = 'cancelled'
                                session.cancel_request = False
                                _logger.debug('DIALER: CANCEL REQUEST')
                                return True
                            else:
                                return False
                                
                        def handle_pause_request():
                            if dialer.pause_request:
                                session.state = 'paused'
                                session.pause_request = False
                                _logger.debug('DIALER: PAUSE REQUEST')
                                return True
                            else:
                                return False
                                
                        def get_current_channels():
                            try:
                                return self.env['asterisk.dialer.channel'].search_count([('session','=',session.id)])
                            except MissingError:
                                return None
                        
                                
                        if handle_cancel_request() or handle_pause_request():
                            exit_stasis_app()
                            return # from start()
                        
                        # Check Stasis App is still there
                        if dialer.dialer_type == 'playback' and not stasis_app_ready.is_set():
                                raise Exception('Stasis App not ready, exit dialer thread.')
                        
                        # Can we add more channels?
                        current_channels = None
                        while current_channels == None:
                            current_channels = get_current_channels()
                            
                        while current_channels >= dialer.simult:
                            
                            self.env.invalidate_all()
                            
                            if handle_cancel_request() or handle_pause_request():
                                exit_stasis_app()
                                return # from start()
                                
                            _logger.debug('DIALER: NO AVAIL CHANNELS, SLEEPING')
                            go_next_call.wait(DIALER_RUN_SLEEP)
                            session.alive_flag = not session.alive_flag # This updates session's write_date
                            # Now refresh current channels
                            current_channels = None
                            while current_channels == None:
                                current_channels = get_current_channels()
                            continue
                                            
                        # update sent call
                        session.sent += 1
                        
                        # Finally place the call
                        originate_call(cdr)


                    # No more contacts, all done
                    session.state = 'done'
                
                except Exception, e:
                    # on client.close() we are always here :-) So just ignore it.
                    if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args[0] == 104: 
                        _logger.debug('DIALER: ARI CLIENT CLOSE')
                        pass
                    else:                        
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        e_txt = '<br/>'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                        # Had to use old api new api compains here about closed cursor :-(
                        if dialer.active_session:
                            dialer.active_session.state = 'error'
                        dialer.message_post(u'%s' % e_txt)
                        _logger.debug(e_txt)
                        try:
                            client.close()
                        except:
                            pass
                    
                
                finally:
                    _logger.debug('DIALER RUN FINALLY')                    
                    
                    if dialer.dialer_type == 'playback':
                        try:
                            ari_client and ari_client.events.userEvent(eventName='exit_request',
                                    application='dialer-%s-session-%s' % (
                                            dialer_id, session.id))
                        except HTTPError:
                            pass
                        except Exception,e:
                            raise
                            
                    new_cr.close()
                    
                    if ari_client:
                        try:
                            ari_client.close()
                        except:
                            pass
                    
                                
                
        
        if self.dialer_type == 'playback':
            # We start Stasis only for playback our file
            stasis_app = threading.Thread(target=run_stasis_app, name='Stasis app thread')
            stasis_app.start()
            
        dialer_worker = threading.Thread(target=run_dialer, name='Run dialer thread')
        dialer_worker.start()   
 
    
    @api.one
    def cancel(self):
        if not self.active_session.state == 'running':
            return

        for t in threading.enumerate():
            if t.name == 'OriginationThread-%s' % self.id:
                if t.is_alive():
                    _logger.debug('FOUND ALIVE THREAD. REQUESTING CANCEL.')
                    self.active_session.cancel_request = True
                    return
                else:
                    break # No need for more iterations

        _logger.debug('FOUND DEAD THREAD OR NO THREAD FOUND. SET CANCELLED.')
        self.active_session.state = 'cancelled'
        self.active_session.cancel_request = False

        
    
    
    @api.one
    def pause(self):
        if not self.active_session.state == 'running':
            return
            
        for t in threading.enumerate():
            if t.name == 'OriginationThread-%s' % self.id:
                if t.is_alive():
                    _logger.debug('FOUND ALIVE THREAD. REQUESTING PAUSE.')
                    self.active_session.pause_request = True
                    return
                else:
                    break # No need for more iterations

        _logger.debug('FOUND DEAD THREAD OR NO THREAD FOUND. SET PAUSED.')
        self.active_session.state = 'paused'
        self.active_session.pause_request = False


    @api.one
    def resume(self):
        if not self.active_session.state in ['paused', 'error']:
            return
        self.start()



SESSION_STATE_CHOICES = (
    ('running', _('Running')),
    ('done', _('Done')),
    ('cancelled', _('Cancelled')),
    ('paused', _('Paused')),
    ('error', _('Error')),
)

class session(models.Model):
    """
    This model holds dialer sessions. 
    Dialer session is created when dialer is started and is closed when dialing 
    process is finished or cancelled. 
    When dialer is paused session is kept open for later resume.
    """
    _name = 'asterisk.dialer.session'
    _order = 'create_date desc'
    _rec_name = 'start_time'
    
    dialer = fields.Many2one('asterisk.dialer', string=_('Dialer'), ondelete='cascade')
    cdrs = fields.One2many('asterisk.dialer.cdr', 'session')
    state = fields.Selection(SESSION_STATE_CHOICES, string=_('State'),
        track_visibility='onchange', default='running')
    progress = fields.Integer(compute='_get_progress', string=_('Progress'))
    total = fields.Integer(string=_('Total'), default=0)
    sent = fields.Integer(string=_('Sent'), default=0)
    answer = fields.Integer(string=_('Answered'), default=0)
    busy = fields.Integer(string=_('Busy'), default=0)
    congestion = fields.Integer(string=_('Congestion'), default=0)
    noanswer = fields.Integer(string=_('No answer'), default=0)
    chanunavail = fields.Integer(string=_('Chanunavail'), default=0)
    cancel_request = fields.Boolean(default=False)
    pause_request = fields.Boolean(default=False)
    start_time = fields.Datetime(string=_('Started'), default=datetime.datetime.now())
    end_time = fields.Datetime(string=_('Ended'))
    
    
    @api.multi
    @api.onchange('state')
    def _on_state_change(self):
        _logger.debug('SESSION STATE CHANGE: %s' % self.state)
        for rec in self:
            if rec.state == 'running':
                rec.start_time = datetime.datetime.now()
            elif rec.state in ['done', 'cancelled']:
                rec.end_time = datetime.datetime.now()

    
    @api.one
    def _get_progress(self):
        self.progress = float(self.sent)/self.total*100 if self.total > 0 else 0



class channel(models.Model):
    _name = 'asterisk.dialer.channel'
    
    dialer = fields.Many2one('asterisk.dialer', ondelete='cascade', string=_('Dialer'))
    session = fields.Many2one('asterisk.dialer.session', string=_('Session'), ondelete='cascade')
    channel_id = fields.Char(select=1)
    other_channel_id = fields.Char(select=1)
    name = fields.Char(string=_('Name'), select=1)
    phone = fields.Char(string=_('Phone'), select=1)
    start_time = fields.Datetime(string=_('Call started'), select=1)
 


CDR_CHOICES = (
    ('process', _('Process')),
    ('queue', _('Queued')),
    # Upper case are ${DIALSTATUS$} from Asterisk
    ('ANSWER', _('Answer')),
    ('BUSY', _('Busy')),
    ('CONGESTION', _('Congestion')),
    ('NOANSWER', _('No answer')),
    ('CHANUNAVAIL', _('Channel unavailable')),
    ('CANCEL', _('Cancel')),
)


class cdr(models.Model):
    _name = 'asterisk.dialer.cdr'
    _rec_name = 'phone'
    
    session = fields.Many2one('asterisk.dialer.session', string=_('Session'), ondelete='cascade')
    dialer = fields.Many2one('asterisk.dialer', ondelete='cascade', string=_('Dialer'))
    channel_id = fields.Char(select=1)
    other_channel_id = fields.Char(select=1)
    name = fields.Char(string=_('Name'), select=1)
    phone = fields.Char(string=_('Phone'), select=1)
    status = fields.Selection(CDR_CHOICES, select=1, string=_('Status'))
    start_time = fields.Datetime(string=_('Started'), select=1)
    end_time = fields.Datetime(string=_('Ended'), select=1)
    answered_time = fields.Integer(string=_('Answer seconds'), select=1)
    answered_time_str = fields.Char(compute='_get_answered_time_str', 
        select=1, string=_('Answer time'))
    playback_start_time = fields.Datetime(string=_('Playback started'), select=1)
    playback_end_time = fields.Datetime(string=_('Playback ended'), select=1)
    #playback_duration = fields.Integer(compute='_get_playback_duration', string=_('Duration'))
    playback_duration_str = fields.Char(compute='_get_playback_duration_str', string=_('Play duration'))
    
    
    @api.one
    def _get_answered_time_str(self):
        # Get nice 00:00:03 string
        if self.answered_time == None:
            self.answered_time_str = ''
        else:
            self.answered_time_str = datetime.timedelta(seconds=self.answered_time).__str__()
    
    
    @api.one
    def _get_playback_duration_str(self):
        # Get nice 00:00:03 string
        if not (self.playback_start_time and self.playback_end_time):
            self.playback_duration_str = ''
        else:
            start_time = datetime.datetime.strptime(self.playback_start_time, '%Y-%m-%d %H:%M:%S')
            end_time = datetime.datetime.strptime(self.playback_end_time, '%Y-%m-%d %H:%M:%S')
            delta = end_time-start_time
            self.playback_duration_str = datetime.timedelta(seconds=delta.seconds).__str__()
        

class phone_group(models.Model):
    _name = 'asterisk.dialer.phone.group'

    name = fields.Char(required=True, string=_('Name'))
    #subscriber_count = fields.Integer(compute='_subscriber_count', string=_('Subscribers count'))
    #subscribers = fields.One2many('asterisk.dialer.subscriber', 'subscriber_list')
    
    #@api.one
    #def _subscriber_count(self):
    #    self.subscriber_count = self.env['asterisk.dialer.subscriber'].search_count(
    #        [('subscriber_list.id', '=', self.id)]) if self.id else 0
            


class phone(models.Model):
    _name = 'asterisk.dialer.phone'
    _order = 'name, phone'
    
    #subscriber_list = fields.Many2one('asterisk.dialer.subscriber.list', 
    #    required=True, ondelete='cascade')
    phone = fields.Char(string=_('Phone number'), required=True)
    name = fields.Char(string=_('Person name'))
    groups = fields.Many2many(comodel_name='asterisk.dialer.phone.group',
                            relation='asterisk_dialer_phone_groups')
    group_names = fields.Char(compute='_get_group_names', store=True, select=1)
    
    #@api.model
    #def _get_latest_list(self):
    #    latest = self.env['asterisk.dialer.subscriber.list'].search([], limit=1, order='id desc')
    #    return latest if latest else False


    #_defaults = {
    #    'subscriber_list': _get_latest_list,
    #}
    @api.one
    @api.depends('groups')
    def _get_group_names(self):
        self.group_names = ', '.join([group.name for group in self.groups]) if self.groups else 'No group'



class dialer_contacts(models.Model):
    _name = 'asterisk.dialer.contacts'
    _order = 'name'
    
    name = fields.Char(required=True)    
    model = fields.Selection((('res.partner', _('Contacts')), 
                        ('asterisk.dialer.phone', _('Phones'))),
                        required=True, default='res.partner')
    model_domain = fields.Char(required=True, string='Selection') 
    note = fields.Text()
    total_count = fields.Char(compute='_get_total_count', store=True, 
                                string='Total')
    
    @api.one
    @api.depends('model', 'model_domain')
    def _get_total_count(self):
        self.total_count = self.env[self.model].search_count(eval(self.model_domain)) if (
                                    self.model_domain and self.model) else '0'
    

class queue(models.Model):
    _name = 'asterisk.dialer.queue'
    _log_access = False
    
    phone = fields.Char(string=_('Phone number'), required=True, select=0)
    name = fields.Char(string=_('Person name'), select=0)
    

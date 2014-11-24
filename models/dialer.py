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
from websocket import WebSocketConnectionClosedException

_logger = logging.getLogger(__name__)

DIALER_RUN_SLEEP = 3 # Dialer thread sleep seconds 


def format_exception():
    """
    Print traceback on handled exceptions.
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()
    return  traceback.format_exception(exc_type, exc_value, exc_traceback)



class AriOdooSessionThread(threading.Thread):
        
    def __init__(self, name, dbname, uid, session_id):
        super(AriOdooSessionThread, self).__init__()
        self.setName(name)
        # Init Environment
        self.cursor = sql_db.db_connect(dbname).cursor()
        self.env = api.Environment(self.cursor, uid, {})
        self.env.cr.autocommit(True)
        self.session = self.env['asterisk.dialer.session'].browse([session_id])
        self.dialer = self.session.dialer
        
        # Init ARI
        server = self.env['asterisk.server.settings'].browse([1])
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str('http://' + server.ip_addr + ':' + server.http_port)
        self.ari_url = ari_url
        self.ari_user = ari_user
        self.ari_pass = ari_pass
        self.ari_client = ari.connect(ari_url, ari_user, ari_pass)


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

    
    def get_active_channels(self):
        return self.env['asterisk.dialer.channel'].search(
                                                [('dialer','=',self.dialer.id)])


    def cancel_calls(self):
        for channel in self.get_active_channels():
            try:
                ari_chan = self.ari_client.channels.get(channelId=channel.channel_id)
                ari_chan.hangup()
                _logger.debug('CANCEL CALLS: Hangup channel: %s' % channel.channel_id)
            except HTTPError:
                _logger.warn('CANCEL CALLS: Channel not found: %s' % channel.channel_id)
            # Remove channel from Odoo
            channel.unlink()


    def ari_user_event(self, event_name):
        self.ari_client.events.userEvent(eventName=event_name,
                                        application ='dialer-%s-session-%s' % (
                                            self.dialer.id, self.session.id))
            



class StasisThread(AriOdooSessionThread):
    """
    Terminate conditions:
    1) No more calls to originate and no more channels are expected to come 
        into Stasis: wait for the last call to hangup and exit.
    2) Origination thread exits due to Pause / Cancel
    Ignore errors:
    1) On answer / playback / hangup events
    """

    def run(self):

        def user_event(channel, ev):
            if ev['eventname'] == 'exit_request':
                # Immediate exit
                self.ari_client.close()

            """
            elif ev['eventname'] == 'originate_complete':
                self.ari_client.close()

            elif ev['eventname'] == 'pause_request':
                self.ari_client.close()
            """
        
        def stasis_start(channel, ev):
        
            def answer_channel(channel):
            
                def playback_finished(playback, event):
                            
                    def hangup():
                        try: 
                            channel.hangup()                        
                        
                        except HTTPError:
                            pass # The call was hangup on other side
                    
                    t = threading.Timer(1, hangup)
                    t.start()

                try:            
                    channel.answer()
                    playback_id = str(uuid.uuid4())                    
                    playback = channel.play(channelId=channel.id, media='sound:%s' % self.sound_file)
                    playback.on_event('PlaybackFinished', playback_finished)        
                
                except HTTPError:
                    pass # The call was hangup on other side

                                
            # Stasis start 
            t = threading.Timer(1, answer_channel, [channel])
            t.start()
                
        # Run
        with api.Environment.manage():

            try:
                self.sound_file = os.path.splitext(self.dialer.sound_file.get_full_path()[0])[0]
                self.ari_client.on_channel_event('StasisStart', stasis_start)
                self.ari_client.on_channel_event('ChannelUserevent', user_event)
                self.ari_client.run(apps='dialer-%s-session-%s' % (self.dialer.id, self.session.id))

            except (ConnectionError, WebSocketConnectionClosedException), e:
                # Asterisk crash or restart?
                self.origination_thread.stasis_app_error.set()
                _logger.debug('STASIS: WebSocketConnectionClosedException - exiting Srasis thread.')
                _logger.debug(format_exception())
                return

            except Exception, e:
                # on ari_client.close() we are here :-) Ugly :-)
                if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args and e.args[0] == 104: 
                    pass
                else:
                    print 'HERE'
                    raise
            
            finally:                
                try:                
                    _logger.debug('STASIS FINALLY CLOSING.')
                    self.cursor.close()
                    self.ari_client.close()                    
                except: pass



class OriginationThread(AriOdooSessionThread):

    go_next_call = threading.Event()
    stasis_app_error = threading.Event()
    
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


    def wait_for_last_call(self):
        while True:
            chan_count = self.get_channel_count()
            if chan_count:
                _logger.debug('WAITING FOR LAST CALL, %s CALLS STILL ACTIVE.' %
                                                                    chan_count)
                time.sleep(1)
            else:
                _logger.debug('WAIT FOR LAST CALLS: NO ACTIVE CALLS. RETURN.')
                return


        
    def run(self):
        """
        Main thread loop. Condition to terminate:
        1) All done. Also terminate Stasis app.
        2) Stasis app not ready (when dialer type is playback)
        3) ARI connection error. Also terminate Stasis app.
        4) Pause / cancel requested. Also terminate Stasis app.
        """
        self.stasis_app_error.clear()
        with api.Environment.manage():
        
            try:
                cdrs = self.env['asterisk.dialer.cdr'].search(
                                            [('session','=',self.session.id),
                                            ('status','=','queue')])
                _logger.debug('CDRS LEN: %s' % len(cdrs))
                
                self.cdrs = iter(cdrs)
                attempts = self.dialer.attempts
                while True:
                    try:
                        # Paranoid but sometimes it does not see changes!
                        self.dialer.invalidate_cache()
                        self.session.invalidate_cache()
                        self.env.invalidate_all()

                        # Reset flag on every round
                        self.go_next_call.clear()

                        # Check for cancel request or stasis app error
                        if self.session.cancel_request or (
                                self.dialer.dialer_type == 'playback' and \
                                self.stasis_app_error.is_set()):
                            _logger.debug('DIALER: CANCEL / ERROR REQUEST')
                            self.cancel_calls()
                            self.dialer.dialer_type == 'playback' and \
                                                self.ari_user_event('exit_request')                            
                            self.session.state = 'cancelled'
                            self.session.cancel_request = False
                            self.dialer.active_session = None
                            return

                        # Check for pause request
                        elif self.session.pause_request:                           
                            _logger.debug('DIALER: PAUSE REQUEST')
                            if self.dialer.dialer_type == 'playback':
                                self.wait_for_last_call()
                                self.ari_user_event('exit_request')
                            self.session.state = 'paused'
                            self.session.pause_request = False
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
                        #attempts -= 1
                        #if not attempts:
                        if True: # Get here for now. Multiple rounds not implemented yet.
                            # We get here only after the last call was hangup in Asterisk
                            # via /channel_update controller. So signal to ARI to exit.                
                            self.session.state = 'done'
                            self.dialer.active_session = None              
                            _logger.debug('CDR StopIteration.')
                            if self.dialer.dialer_type == 'playback':
                                self.wait_for_last_call()
                                self.ari_user_event('exit_request')
                            return
                        
                        else:
                            # Let have a next round for unsuccessful calls
                            _logger.debug('ROUND DONE. GOING ROUND %s FROM %s.' %
                                        (attempts, self.dialer.attempts))
                            cdrs = self.env['asterisk.dialer.cdr'].search(
                                            [('session','=',self.session.id),
                                            ('status','!=','ANSWER')])
                            self.cdrs = iter(cdrs)
                            continue
                

            except (ConnectionError, HTTPError), e:
                # ARI Error
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

    name = fields.Char(required=True, string=_('Name'))
    description = fields.Text(string=_('Description'))
    dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'),
                                    default='playback')
    context_name = fields.Char(string=_('Context name'), default='')
    state = fields.Html(compute='_get_state', string=_('State'))
    active_session = fields.Many2one('asterisk.dialer.session')
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
    simult = fields.Integer(string=_('Simultaneous Calls'), default=1)
    attempts = fields.Integer(string=_('Call Attempts'), default=1)
    cancel_request = fields.Boolean(related='active_session.cancel_request')
    pause_request = fields.Boolean(related='active_session.pause_request')
    is_origination_thread_alive = fields.Boolean(compute='_is_origination_thread_alive')


    @api.one
    def _get_cdr_count(self):
        self.cdr_count = self.env['asterisk.dialer.cdr'].search_count([('dialer','=',self.id)])
    
    
    @api.one
    def _get_state(self):
        """
        Return html code according to active session state
        """
        state = self.active_session_state
        if state == 'done':
            glyphicon = 'ok'
        elif state == 'cancelled':
            glyphicon = 'eject'
        elif state == 'paused':
            glyphicon = 'time'
        elif state == 'running':
            glyphicon = 'bullhorn'
        elif state == 'error':
            glyphicon = 'exclamation-sign'
        else:
            glyphicon = 'edit'

        self.state = "<span class='glyphicon glyphicon-%s'/>" % glyphicon
        
        
    @api.one
    @api.depends('channels')
    def _get_channel_count(self):
        self.channel_count = len(self.channels)


    @api.one
    def _is_origination_thread_alive(self):
        for t in threading.enumerate():
            if t.name == 'OriginationThread-%s' % self.id:
                if t.is_alive():
                    _logger.debug('ORIGINATE THREAD IS ALIVE')
                    return True
                else:
                    _logger.debug('ORIGINATE THREAD IS DEAD')
                    return False
        # No origination thread at all
        _logger.debug('NO ORIGINATION THREAD FOUND.')
        return False
  
  
    @api.one
    def start(self):
        if self.active_session_state == 'running':
            return
        # Validations before start
        if not self.contacts:
            raise ValidationError(_('You have nobody to dial. Add contacts first :-)'))
        elif self.dialer_type == 'playback' and not self.sound_file:
            raise ValidationError(_('Dialer type is Playback and sound file not set!'))
        elif self.dialer_type == 'dialplan' and not self.context_name:
            raise ValidationError(_('Dialer type is Dialplan and Asterisk context not set!'))

        # Get / create active session
        session = self.active_session
        
        if not session:
            _logger.debug('NO INTERRUPTED SESSION, CREATING ONE.')
            self.env.cr.autocommit(False)
            session = self.env['asterisk.dialer.session'].create(
                                                        {'dialer': self.id})
            self.active_session = session
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

        # Init threads
        stasis_thread = origination_thread = None
        try:
            origination_thread = OriginationThread('OriginationThread-%s' % 
                                            self.id, self.env.cr.dbname,
                                            self.env.uid, session.id)

            if self.dialer_type == 'playback':
                stasis_thread = StasisThread('StasisThread-%s' % self.id,
                                            self.env.cr.dbname, self.env.uid, 
                                            session.id)
                stasis_thread.origination_thread = origination_thread
                origination_thread.stasis_thread = stasis_thread

        except ConnectionError:
            del stasis_thread
            del origination_thread
            raise ValidationError(_('Cannot connect to Asterisk. Check that ' 
                            'Asterisk is running and ARI settings are valid.'))
        
        # Start threads
        stasis_thread and stasis_thread.start()
        origination_thread.start()


    
    @api.one
    def cancel(self):
        if self.active_session_state not in ['running','paused','error']:
            return

        for t in threading.enumerate():
            if t.name == 'OriginationThread-%s' % self.id:
                if t.is_alive():
                    _logger.debug('FOUND ALIVE THREAD. REQUESTING CANCEL.')
                    self.active_session.cancel_request = True                    
                    self.env.cr.commit()                    
                    return
                else:
                    break # No more iterations as we found our dead thread.

        _logger.debug('FOUND DEAD THREAD OR NO THREAD FOUND. SET CANCELLED.')
        self.active_session.write({'state': 'cancelled',
                                    'cancel_request': False,
                                    'pause_request': False
        })
        self.active_session = None
        self.env.cr.commit()


        
    
    @api.one
    def pause(self):
        if not self.active_session_state == 'running':
            return

        for t in threading.enumerate():
            if t.name == 'OriginationThread-%s' % self.id:
                if t.is_alive():
                    _logger.debug('FOUND ALIVE THREAD. REQUESTING PAUSE.')
                    self.active_session.pause_request = True
                    self.env.cr.commit()
                    return
                else:
                    break # No need for more iterations

        _logger.debug('FOUND DEAD THREAD OR NO THREAD FOUND. SET PAUSED.')
        self.active_session.state = 'paused'
        self.active_session.pause_request = False


    @api.one
    def resume(self):
        if self.active_session_state not in ['paused', 'error']:
            _logger.debug('NOT RESUMING, STATE IS: %s' % self.active_session_state)
            return
        _logger.debug('RESUMING.')
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
    cancel = fields.Integer(string=_('Cancel'), default=0)
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
    duration = fields.Char(compute='_get_duration')


    @api.one
    def _get_duration(self):
        start_time = datetime.datetime.strptime(self.start_time, '%Y-%m-%d %H:%M:%S')
        self.duration = (datetime.datetime.now() - start_time).seconds
        

    @api.one
    def hangup_call(self):
        server = self.env['asterisk.server.settings'].browse([1])
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str('http://' + server.ip_addr + ':' + server.http_port)
        
        try:
            ari_client = ari.connect(ari_url, ari_user, ari_pass)
            ari_chan = ari_client.channels.get(channelId=self.channel_id)
            ari_chan.hangup()
            _logger.debug('HANGUP CHANNEL: %s' % self.channel_id)
            self.unlink()
        
        except HTTPError:
            _logger.warn('CHANNEL NOT FOUND, REMOVING FROM ACTIVE: %s' % self.channel_id)
            # Remove channel from Odoo
            self.unlink()

        except ConnectionError:
            raise ValidationError('Cannot connect to Asterisk. Check Settings.')
 


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
    

"""
class provider(models.Model):
    _name = 'asterisk.dialer.provider'
    _rec_name = 'peer_name'

    prefix = fields.Char(required=True, default='[0-9]+')
    simult = fields.Integer(required=True, default=1)
    note = fields.Text()

"""

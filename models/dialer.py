# -*- coding: utf-8 -*-
import ari
import datetime
import logging
import time
import threading
import os
import sys
import traceback
import uuid
from openerp import fields, models, api, sql_db, _
from openerp.exceptions import ValidationError, DeferredException, MissingError
from requests.exceptions import HTTPError, ConnectionError
from websocket import WebSocketConnectionClosedException

_logger = logging.getLogger(__name__)

# Dialer thread sleep seconds
DIALER_RUN_SLEEP = 3


def format_exception():
    """
    Print traceback on handled exceptions.
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()
    return ''.join(s for s in traceback.format_exception(
        exc_type, exc_value, exc_traceback) if s != '\n')


class AriOdooSessionThread(threading.Thread):
    def __init__(self, name, dialer):
        super(AriOdooSessionThread, self).__init__()
        dbname = dialer.env.cr.dbname
        uid = dialer.env.uid
        # Init new Env
        self.cursor = sql_db.db_connect(dbname).cursor()
        self.env = api.Environment(self.cursor, uid, {})
        self.env.cr.autocommit(True)
        # Init objects with new Env
        self.dialer = self.env['asterisk.dialer'].browse([dialer.id])
        self.session = self.env['asterisk.dialer.session'].browse(
            [dialer.active_session.id])
        self.setName('%s-%s' % (name, self.dialer.id))
        # Init ARI
        server = self.env['asterisk.server.settings'].browse([1])
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str('http://' + server.ip_addr + ':' + server.http_port)
        self.ari_url = ari_url
        self.ari_user = ari_user
        self.ari_pass = ari_pass

    def get_channel_count(self):
        channel_count = None
        while channel_count is None:
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
            [('dialer', '=', self.dialer.id)])

    def cancel_calls(self):
        for channel in self.get_active_channels():
            try:
                ari_chan = self.ari_client.channels.get(
                    channelId=channel.channel_id)
                ari_chan.hangup()
                _logger.debug(
                    'CANCEL CALLS: Hangup channel: %s' % channel.channel_id)
            except (MissingError, HTTPError):
                _logger.warn(
                    'CANCEL CALLS: Channel not found: %s' % channel.channel_id)
            # Channel will be unlinked from CDR update

    def ari_user_event(self, event_name):
        self.ari_client.events.userEvent(
            eventName=event_name,
            application='dialer-%s-session-%s' % (
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

    ext_event_handlers = []

    def __init__(self, name, dialer):
        super(StasisThread, self).__init__(name, dialer)
        for event, handler in dialer.stasis_event_handlers:
            self.ext_event_handlers.append([event, handler])

    def run(self):

        def user_event(channel, ev):
            if ev['eventname'] == 'exit_request':
                # Immediate exit
                self.ari_client.close()

        def stasis_start(channel, ev):

            def answer_channel(channel):

                def playback_finished(playback, event):

                    def hangup():
                        try:
                            channel.hangup()
                        except HTTPError:
                            # The call was hangup on other side
                            pass

                    timer = threading.Timer(1, hangup)
                    timer.start()

                try:
                    channel.answer()
                    playback_id = str(uuid.uuid4())
                    playback = channel.play(
                        channelId=channel.id,
                        media='sound:%s' % self.sound_file)
                    playback.on_event('PlaybackFinished', playback_finished)

                except HTTPError:
                    # The call was hangup on other side
                    pass

            # Stasis start
            timer = threading.Timer(1, answer_channel, [channel])
            timer.start()

        # Run
        # Check Dialer type
        if self.dialer.dialer_type != 'stasis':
            _logger.debug('DIALER TYPE IS NOT STASIS, NOT STARTING.')
            return

        with api.Environment.manage():

            try:
                self.ari_client = ari.connect(
                    self.ari_url, self.ari_user, self.ari_pass)
                self.sound_file = os.path.splitext(
                    self.dialer.sound_file.get_full_path()[0])[0]
                self.ari_client.on_channel_event('StasisStart', stasis_start)
                self.ari_client.on_channel_event(
                    'ChannelUserevent', user_event)

                # Add extension event handlers
                for event, handler in self.ext_event_handlers:
                    _logger.debug(
                        'INSTALLING STASIS EVENT HANDLER FOR: %s' % event)
                    self.ari_client.on_channel_event(event, handler)
                self.ari_client.run(apps='dialer-%s-session-%s' % (
                    self.dialer.id, self.session.id))

            except (ConnectionError, WebSocketConnectionClosedException), e:
                # Asterisk crash or restart?
                # Try to get OriginationThread
                for thread in threading.enumerate():
                    if (thread.name == 'OriginationThread-%s' % self.dialer.id
                            and thread.is_alive()):
                        _logger.debug(
                            'SETTINGS stasis_app_error IN ORIGINATION THREAD.')
                        thread.stasis_app_error.set()

                _logger.debug(
                    'STASIS: WebSocketConnectionClosedException '
                    '- exiting Stasis thread.')
                _logger.debug(format_exception())
                return

            except Exception as e:
                # on ari_client.close() we are here :-) Ugly :-)
                if hasattr(e, 'args') and type(e.args) in (list, tuple)\
                and e.args and e.args[0] == 104:
                    pass
                else:
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
            'peer': contact['peer_id'],
            'channel_id': channel_id,
            'other_channel_id': otherchannel_id,
            'phone': contact['phone'],
            'start_time': datetime.datetime.now(),
            'name': contact['name']})
        _logger.debug('CHANNELS CREATED: %s, %s.' % (channel_id, otherchannel_id))
        return channel_id, otherchannel_id


    def update_cdr(self, contact, channel_id, otherchannel_id):
        """
        Update a CDR created before on session create.
        """
        cdr = self.env['asterisk.dialer.cdr'].search([('id','=', contact['cdr_id'])])
        if cdr:
            cdr.write({
                    'channel_id': channel_id,
                    'other_channel_id': otherchannel_id,
                    'status': 'process',
                    'start_time': datetime.datetime.now(),
                    'peer': contact['peer_id'],
                    })
            _logger.debug('CDR UPDATED: %s, %s, %s.' %(contact['phone'], channel_id, otherchannel_id))
        else:
            _logger.warn('CDR not found! Phone: %s.' % contact['phone'])


    def originate_call(self, contact):
        """
        ARI call origination method.
        """
        channel_id, otherchannel_id = self.create_channel(contact)
        self.update_cdr(contact, channel_id, otherchannel_id)
        
        if self.dialer.dialer_type == 'stasis':
            ari_channel = self.ari_client.channels.originate(
                        endpoint='Local/%s@%s' % (contact['phone'], contact['peer_name']),
                        app='dialer-%s-session-%s' % (self.dialer.id, self.session.id),                        
                        channelId=channel_id,
                        otherChannelId=otherchannel_id)
        else:
            ari_channel = self.ari_client.channels.originate(
                        endpoint='Local/%s@%s' % (contact['phone'], contact['peer_name']),
                        context='%s' % self.dialer.context_name, extension='%s' % contact['phone'], priority='1',
                        channelId=channel_id,
                        otherChannelId=otherchannel_id)

        # Increment sent counter
        self.session.sent += 1
        _logger.debug('CALL ORIGINATED: %s' % contact['phone'])


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
        2) Stasis app not ready (when dialer type is stasis)
        3) ARI connection error. Also terminate Stasis app.
        4) Pause / cancel requested. Also terminate Stasis app.
        """
        self.stasis_app_error.clear()
        with api.Environment.manage():
        
            try:
                # Connect to ARI before starting thread.
                self.ari_client = ari.connect(self.ari_url, self.ari_user, self.ari_pass)

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
                                self.dialer.dialer_type == 'stasis' and \
                                self.stasis_app_error.is_set()):
                            _logger.debug('DIALER: CANCEL / ERROR REQUEST')
                            self.cancel_calls()
                            self.dialer.dialer_type == 'stasis' and \
                                                self.ari_user_event('exit_request')                            
                            self.session.state = 'cancelled'
                            self.session.cancel_request = False
                            self.dialer.active_session = None
                            return

                        # Check for pause request
                        elif self.session.pause_request:                           
                            _logger.debug('DIALER: PAUSE REQUEST')
                            if self.dialer.dialer_type == 'stasis':
                                self.wait_for_last_call()
                                self.ari_user_event('exit_request')
                            self.session.state = 'paused'
                            self.session.pause_request = False
                            return

                        # Check if we can add more calls 
                        self.env.cr.execute("""SELECT 
                                                cdr.id AS cdr_id,
                                                cdr.phone AS phone,
                                                COALESCE(cdr.name, '') AS name,
                                                peer.id AS peer_id,
                                                peer.name AS peer_name,
                                                route.pattern AS pattern
                                        FROM 
                                            asterisk_dialer_peer peer,
                                            asterisk_dialer_route route,
                                            asterisk_dialer_cdr cdr
                                    WHERE 
                                        cdr.status = 'queue' AND 
                                        route.dialer = %s AND
                                        cdr.session = %s AND
                                        peer.simult > 
                            (SELECT COUNT(*) FROM asterisk_dialer_channel chan 
                                                    WHERE peer = peer.id) AND
                                        peer.id = route.peer AND
                                        cdr.phone LIKE CONCAT(route.pattern,'%%')
                                    ORDER BY
                                        cdr.id, route.sequence
                                    LIMIT 1
                            """ % (self.dialer.id, self.session.id)
                        )
                        cdr = self.env.cr.dictfetchall()

                        if cdr:
                            _logger.debug('CDR FOR CALL: %s' % cdr[0])
                            self.originate_call(cdr[0])

                        elif self.session.cdr_queue_count == 0:
                            # All done
                            raise StopIteration

                        else:                        
                            _logger.debug('NO CHANNELS AVAILABLE, SLEEPING.')
                            # Sleep here or be interrupted by hangup 
                            self.go_next_call.wait(DIALER_RUN_SLEEP)

                    except StopIteration:                        
                        _logger.debug('CDR StopIteration.')
                        if self.dialer.dialer_type == 'stasis':
                            self.wait_for_last_call()
                            self.ari_user_event('exit_request')
                            self.session.state = 'done'
                            self.dialer.active_session = None
                        return

            except (ConnectionError, HTTPError), e:
                # ARI Error
                self.session.state = 'error'
                self.dialer.message_post('ARI ConnectionError: %s' % e.message)
                _logger.debug(format_exception())

            finally:
                try:
                    self.cursor.close()
                    self.ari_client.close()
                    _logger.debug('ORIGINATE FINALLY CLOSING.')
                except:
                    pass


DIALER_TYPE_CHOICES = (
    ('stasis', _('Odoo Stasis App')),
    ('dialplan', _('Asterisk Dialplan')),
)


class dialer(models.Model):
    _name = 'asterisk.dialer'
    _inherit = 'mail.thread'
    _description = 'Asterisk Dialer'
    _order = 'name'

    stasis_thread = None
    origination_thread = None
    stasis_event_handlers = []


    name = fields.Char(required=True, string=_('Name'),
        help='Dialer name, can be any string.')
    description = fields.Text(string=_('Description'))
    dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'),
                                    default='stasis')
    context_name = fields.Char(string=_('Context name'), default='')
    state = fields.Html(compute='_get_state', string=_('State'))
    active_session = fields.Many2one('asterisk.dialer.session')
    active_session_state = fields.Selection(related='active_session.state')
    active_session_progress = fields.Integer(related='active_session.progress')
    sessions = fields.One2many('asterisk.dialer.session', 'dialer')
    sound_file = fields.Many2one('asterisk.dialer.soundfile', string=_('Sound file'),
        ondelete='restrict')
    #start_time = fields.Datetime(string=_('Start time'), 
    #    help=_('Exact date and time to start dialing. For scheduled dialers.'))
    from_time = fields.Float(digits=(2, 2), string=_('From time'), default=10.00,
        help=_('Time permitted for calling If dialer is paused it will be resumed this time.')) 
    to_time = fields.Float(digits=(2, 2), string=_('To time'), default=18.00,
        help=_('Time perimitted for calling. If dialer is running it will be paused this time')) 
    contacts = fields.Many2many(comodel_name='asterisk.dialer.contact',
                                relation='asterisk_dialer_contacts_rel')
    contact_count = fields.Integer(compute='_get_contact_count', string=_('Total Contacts'))
    channels = fields.One2many('asterisk.dialer.channel', 'dialer', string=_('Current Calls'))
    channel_count = fields.Integer(compute='_get_channel_count')
    cdrs = fields.One2many('asterisk.dialer.cdr', 'dialer', string=_('Call Detail Records'))
    cdr_count = fields.Integer(compute='_get_cdr_count', string=_('Number of Call Detail Records'))
    routes = fields.One2many(comodel_name='asterisk.dialer.route', inverse_name='dialer')
    route_count = fields.Integer(compute='_get_route_count', string='Route Count')
    peer_names = fields.Char(compute='_get_peer_names')
    attempts = fields.Integer(string=_('Call Attempts'), default=1)
    cancel_request = fields.Boolean(related='active_session.cancel_request')
    pause_request = fields.Boolean(related='active_session.pause_request')    


    @api.one
    def _get_cdr_count(self):
        self.cdr_count = self.env['asterisk.dialer.cdr'].search_count([('dialer','=',self.id)])


    @api.one
    def _get_route_count(self):
        self.route_count = len(self.routes)


    @api.one
    def _get_peer_names(self):
        names = []
        for route in self.routes:
            if route.peer.name not in names:
                names.append(route.peer.name)
        self.peer_names = ', '.join(names) 


    @api.one
    @api.onchange('contacts')
    def _get_contact_count(self):
        total_count = 0
        for group in self.contacts:
            total_count += self.env[group.model].search_count(eval(group.model_domain))
        self.contact_count = total_count
    
    
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


    def get_stasis_event_handlers(self):
        return self.stasis_event_handlers


    def set_stasis_event_handlers(self, stasis_event_handlers):
        # Overide me in child classes
        self.set_stasis_event_handlers = stasis_event_handlers

    
    def validate_start(self):
        if not self.contacts:
            raise ValidationError(_('You have nobody to dial. Add contacts first :-)'))
        elif self.dialer_type == 'stasis' and not self.sound_file:
            raise ValidationError(_('Dialer type is Stasis and Sound File not set!'))
        elif self.dialer_type == 'dialplan' and not self.context_name:
            raise ValidationError(_('Dialer type is Dialplan and Asterisk context not set!'))

        # Check routing
        invalid_destinations = []
        for dst in xrange(0, 10):
            self.env.cr.execute("""SELECT peer FROM asterisk_dialer_route 
                    WHERE dialer=%s AND '%s%s' like CONCAT(pattern,'%%')""" % (
                                                            self.id, dst, dst)
            )
            peers = self.env.cr.dictfetchall()
            if not peers:
                invalid_destinations.append(str(dst))

        if invalid_destinations:
            raise ValidationError(_("No routes for destinations: %s") % 
                ', '.join(invalid_destinations))



    def prepare_session(self):
        """
        Create or re-use existing session
        """
        session = self.active_session
        self.env.cr.autocommit(False)
        if not session:
            _logger.debug('NO INTERRUPTED SESSION, CREATING ONE.')            
            session = self.env['asterisk.dialer.session'].create(
                                                        {'dialer': self.id})
            self.active_session = session

            total_count = 0
            for group in self.contacts:
                for contact in self.env[group.model].search(
                                                    eval(group.model_domain)):
                    self.env['asterisk.dialer.cdr'].create({
                        'phone': contact.phone,
                        'name': contact.name,
                        'dialer': self.id,
                        'session': session.id,
                        'status': 'queue',
                    })
                    total_count += 1
            session.total = total_count
        
        else:
            # Queue session's unproccessed calls
            session.cdrs.search([('status','=','process')]).write(
                                                    {'status': 'queue'})

        self.env.cr.commit()
        self.env.cr.autocommit(True)
        
        session.state = 'running'
        # Reset channels
        self.channels.unlink()



    @api.one
    def start(self):
        if self.active_session_state == 'running':
            return
        self.validate_start()
        self.prepare_session()
        
        self.origination_thread = OriginationThread('OriginationThread', self)
        self.stasis_thread = StasisThread('StasisThread', self)

        # Start threads
        self.stasis_thread.start()
        self.origination_thread.start()


    @api.one
    def cancel(self):
        if self.active_session_state not in ['running','paused','error']:
            return

        for thread in threading.enumerate():
            if tread.name == 'OriginationThread-%s' % self.id:
                if thread.is_alive():
                    _logger.debug('FOUND ALIVE THREAD. REQUESTING CANCEL.')
                    self.active_session.cancel_request = True
                    self.env.cr.commit()
                    return
                else:
                    # No more iterations as we found our dead thread.
                    break

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

        for thread in threading.enumerate():
            if thread.name == 'OriginationThread-%s' % self.id:
                if thread.is_alive():
                    _logger.debug('FOUND ALIVE THREAD. REQUESTING PAUSE.')
                    self.active_session.pause_request = True
                    self.env.cr.commit()
                    return
                else:
                    # No need for more iterations
                    break

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
    cdr_queue_count = fields.Integer(compute='_get_cdr_queue_count')
    
    
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


    @api.one
    def _get_cdr_queue_count(self):
        self.cdr_queue_count = self.env['asterisk.dialer.cdr'].search_count([
                                        ('session','=',self.id),
                                        ('status','=','queue')
        ])



class channel(models.Model):
    _name = 'asterisk.dialer.channel'
    
    dialer = fields.Many2one('asterisk.dialer', ondelete='cascade', string=_('Dialer'))
    session = fields.Many2one('asterisk.dialer.session', string=_('Session'), ondelete='cascade')
    channel_id = fields.Char(select=1, string='Channel ID')
    other_channel_id = fields.Char(select=1, string='Other Channel ID')
    name = fields.Char(string='Name', select=1)
    phone = fields.Char(string='Phone', select=1)
    start_time = fields.Datetime(string=_('Call started'), select=1)
    duration = fields.Char(compute='_get_duration', string=_('Duration'))
    peer = fields.Many2one(comodel_name='asterisk.dialer.peer', 
                            string='Dial Context', ondelete='set null')


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

        except ConnectionError:
            raise ValidationError('Cannot connect to Asterisk. Check Settings.')
            
        try:
            ari_chan = ari_client.channels.get(channelId=self.channel_id)
            ari_chan.hangup()
            _logger.debug('HANGUP CHANNEL: %s' % self.channel_id)
        
        except HTTPError:
            _logger.warn('CHANNEL NOT FOUND, REMOVING FROM ACTIVE: %s' % self.channel_id)

        finally:
            self.session['cancel'] += 1
            self.unlink()


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
    _order = 'id'
    
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
    peer = fields.Many2one(comodel_name='asterisk.dialer.peer', ondelete='set null', 
                                                string='Dial Context')
    
    
    @api.one
    def _get_answered_time_str(self):
        # Get nice 00:00:03 string
        if self.answered_time == None:
            self.answered_time_str = ''
        else:
            self.answered_time_str = datetime.timedelta(seconds=self.answered_time).__str__()
    
    """
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
    """

        

class phone_group(models.Model):
    _name = 'asterisk.dialer.phone.group'

    name = fields.Char(required=True, string=_('Name'))
    phones = fields.One2many(comodel_name='asterisk.dialer.phone', 
                            inverse_name='group')
    phone_count = fields.Integer(compute='_get_phone_count', 
                                string='Number of phones')


    @api.one
    def _get_phone_count(self):
        self.phone_count = len(self.phones)

            

class phone(models.Model):
    _name = 'asterisk.dialer.phone'
    _order = 'name, phone'
    
    phone = fields.Char(string=_('Phone number'), required=True)
    name = fields.Char(string=_('Person name'))
    group = fields.Many2one(comodel_name='asterisk.dialer.phone.group')                            
    


class dialer_contacts(models.Model):
    _name = 'asterisk.dialer.contact'
    _order = 'name'
    
    name = fields.Char(required=True)    
    model = fields.Selection((('res.partner', _('Contacts')), 
                        ('asterisk.dialer.phone', _('Phones'))),
                        required=True, default='res.partner')
    model_domain = fields.Char(required=True, string='Selection') 
    model_domain_ro = fields.Char(compute='_get_model_domain', string='Current filter')
    note = fields.Text()
    total_count = fields.Char(compute='_get_total_count', store=True, 
                                string='Total')
    
    @api.one
    @api.depends('model', 'model_domain')
    def _get_total_count(self):
        self.total_count = self.env[self.model].search_count(eval(self.model_domain)) if (
                                    self.model_domain and self.model) else '0'


    @api.onchange('model')
    def reset_domain(self):
        self.model_domain = ''
    

    @api.onchange('model_domain')
    def _get_model_domain(self):
        if not self.model_domain:
            self.model_domain_ro = ''
            return
        res = []
        for group in eval(self.model_domain):
            try:
                s1, s2, s3 = group
                s = '(%s %s %s)' % (s1, s2, s3.encode('utf8') if s3 is list else s3)    
                res.append(s)
            except ValueError:
                res.append('|')            
        self.model_domain_ro = ', '.join(res)

    

class peer(models.Model):
    _name = 'asterisk.dialer.peer'
    _order = 'name'

    name = fields.Char(required=True, string='Dial context name')    
    simult = fields.Integer(required=True, default=100,
                                                string='Max simultaneous calls')
    note = fields.Text()
    routes = fields.One2many(comodel_name='asterisk.dialer.route', 
                            inverse_name='peer')


class route(models.Model):
    _name = 'asterisk.dialer.route'
    _rec_name = 'pattern'
    _order = 'pattern, sequence, peer'

    sequence = fields.Integer(required=True, default=1, select=1)
    dialer = fields.Many2one(comodel_name='asterisk.dialer', required=True,
                            ondelete='cascade')
    peer = fields.Many2one(comodel_name='asterisk.dialer.peer', required=True, 
                            ondelete='cascade', string=_('Dial context'))
    pattern = fields.Char(required=True,  help=_(
                                'Valid patterns are digits and _ - any digit.'))
    


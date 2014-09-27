import ari
import datetime
import time
import threading
import sys, traceback
import uuid
from openerp import fields, models, api, sql_db, _
from openerp.exceptions import ValidationError, DeferredException
from requests.exceptions import HTTPError


DIALER_RUN_SLEEP = 5 # Dialer threads sleeps

"""
ari.connect(get_ari_connection_string())
ari.connect('http://localhost:8088', 'dialer', 'test')
"""

DIALER_TYPE_CHOICES = (    
    ('playback', _('Playback message')),
    ('dialplan', _('Asterisk dialplan')),
)
    

class dialer(models.Model):
    _name = 'asterisk.dialer'
    _inherit = 'mail.thread'
    
    @api.model  
    def _get_dialer_model(self):
        dialer_models = (
            ('res.partner', _('Contacts')),
            ('asterisk.dialer.subscriber.list', _('Subscribers list')),
        )
        return dialer_models
        
        
    @api.one
    def _get_cdr_count(self):
        self.cdr_count = self.env['asterisk.dialer.cdr'].search_count([('dialer','=',self.id)])
        
    @api.one
    def _get_state(self):
        """
        Take the latest session. If the session is over e.g. state is done or 
        cancelled we are ready for a new one. Otherwise return session state.
        """
        
        session = self.env['asterisk.dialer.session'].search([('dialer','=',self.id)], order='create_date desc', limit=1)
        if not session or session.state in ['done', 'cancelled']:
            self.state = 'ready'
        else:
            self.state = session.state
        
        
    @api.one
    def _get_active_session(self):
        """Get open session"""
        session = self.sessions.search([
            ('state', 'in', ['running', 'paused'])],
            order='create_date desc', limit=1)
        self.active_session = session


    name = fields.Char(required=True, string=_('Name'))
    description = fields.Text(string=_('Description'))
    dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'))
    context_name = fields.Char(string=_('Context name'))
    state = fields.Char(compute='_get_state', string=_('State'), track_visibility='onchange')
    active_session = fields.Many2one('asterisk.dialer.session', compute='_get_active_session')
    sessions = fields.One2many('asterisk.dialer.session', 'dialer')
    sound_file = fields.Binary(string=_('Sound file'))
    #start_time = fields.Datetime(string=_('Start time'), 
    #    help=_('Exact date and time to start dialing. For scheduled dialers.'))
    from_time = fields.Float(digits=(2, 2), string=_('From time'), 
        help=_('Time permitted for calling If dialer is paused it will be resumed this time.')) 
    to_time = fields.Float(digits=(2, 2), string=_('To time'), 
        help=_('Time perimitted for calling. If dialer is running it will be paused this time')) 
    dialer_model = fields.Selection('_get_dialer_model', required=True, string=_('Dialer model'))
    dialer_domain = fields.Char(string=_('Domain'))
    subscriber_lists = fields.Many2many('asterisk.dialer.subscriber', 'campaign', string=_('Subscribers')) 
    channels = fields.One2many('asterisk.dialer.channel', 'dialer', string=_('Current calls'))
    cdrs = fields.One2many('asterisk.dialer.cdr', 'dialer', string=_('Call Detail Records'))
    cdr_count = fields.Integer(compute='_get_cdr_count', string=_('Phone of call detail records'))
    simult = fields.Integer(string=_('Simultaneous calls'))
    cancel_request = fields.Boolean(related='active_session.cancel_request')
    pause_request = fields.Boolean(related='active_session.pause_request')
  
    _defaults = {
        'dialer_type': 'playback',
        'dialer_model': 'res.partner',
        'state': 'draft',
        'from_time': 10.00,
        'to_time': 18.00,
        'simult': 1,
    }
    
    @api.one
    def start(self):
        self.env.cr.commit()
        if not self.dialer_domain:
            raise ValidationError(_('You have nobody to dial. Add contacts first :-)'))
        
        server = self.env['asterisk.server.settings'].browse([1])
        dialer_context = server.context_name
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str(server.ari_url)
        
        # Get active session
        session = self.sessions.search([
                                ('state', 'in', ['running', 'paused'])],
                                order='create_date desc', limit=1)
                                
        if not session:
            domain = [('phone', '!=', None)] + [eval(self.dialer_domain)[0]]
            contacts = self.env[self.dialer_model].search(domain)
            session = self.env['asterisk.dialer.session'].create({
                'dialer': self.id,
                'total': len(contacts),
            })
            
            for contact in contacts:
                queue = self.env['asterisk.dialer.queue'].create({
                    'session': session.id,
                    'phone': contact.phone,
                    'name': contact.name,
                })
        self.env.cr.commit()
        
        stasis_app_ready = threading.Event()
        go_next_call = threading.Event()
        
        
        
        def run_stasis_app():
            
            def stasis_start(channel, ev):
                pass
                
                
            def application_replaced(app):
                pass


            def user_event(channel, ev):
                if ev['eventname'] == 'exit_request':
                    client.close()
                    
            
            def hangup_request(channel, ev):
                pass
                
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()
            uid, context = self.env.uid, self.env.context
            with api.Environment.manage():
                self.env = api.Environment(new_cr, uid, context)

                try:
                    client = ari.connect(ari_url, ari_user, ari_pass)
                    client.on_channel_event('StasisStart', stasis_start)
                    client.on_event('ApplicationReplaced', application_replaced)
                    client.on_channel_event('ChannelUserevent', user_event)
                    client.on_channel_event('ChannelHangupRequest', hangup_request)
                    stasis_app_ready.set()
                    client.run(apps='dialer-%s-session-%s' % (self.id, session.id))
                except Exception, e:
                    # on client.close() we are always here :-) So just ignore it.
                    if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args[0] == 104: 
                        pass
                    else:
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        e_txt = '<br/>'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                        # Had to use old api new api compains here about closed cursor :-(
                        dialer_obj = self.pool.get('asterisk.dialer')
                        dialer = dialer_obj.browse(new_cr, self.env.uid, [self.id]).message_post('Error:\n%s' % e_txt)
                        new_cr.commit()
                        print e_txt
                        try:
                            client.close()
                        except:
                            pass

                
                finally:
                    print 'STASIS APP FINALLY'
                    # If an error happens in Stasis app thread 
                    # let Dialer run thread about it so that it could exit
                    new_cr.commit()
                    new_cr.close()
 
            
            
        def run_dialer():
            
            def originate_call(contact):
                # Generate channel ids
                chan_id = uuid.uuid1()
                channelId = '%s-1' % chan_id
                otherChannelId = '%s-2' % chan_id
            
                if self.dialer_type == 'playback':
                    ari_channel = ari_client.channels.originate(
                        endpoint='Local/%s@dialer' % (contact.phone),
                        app='dialer-%s-session-%s' % (self.id, session.id),                        
                        channelId=channelId,
                        otherChannelId=otherChannelId)
                else:
                    ari_channel = ari_client.channels.originate(
                        endpoint='Local/%s@dialer' % (contact.phone),                        
                        context='%s' % self.context_name, extension='%s' % contact.phone, priority='1',
                        channelId=channelId,
                        otherChannelId=otherChannelId)
                
                # +1 current call
                channel = self.env['asterisk.dialer.channel'].create({
                    'dialer': self.id,
                    'session': session.id,
                    'channel_id': channelId,
                    'other_channel_id': otherChannelId,
                    'phone': contact.phone,
                    'start_time': datetime.datetime.now(),
                    'name': contact.name})
                self.env.cr.commit()
                
                # +1 sent call
                session.sent = session.sent + 1
                self.env.cr.commit()
                
                # Create CDR
                cdr = self.env['asterisk.dialer.cdr'].create({
                    'dialer': self.id,
                    'channel_id': channelId,
                    'other_channel_id': otherChannelId,
                    'phone': contact.phone,
                    'name': contact.name,
                    'status': 'PROGRESS',
                    'start_time': datetime.datetime.now(),
                    })
                self.env.cr.commit()

            uid, context = self.env.uid, self.env.context
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()  
            with api.Environment.manage():                                  
                self.env = api.Environment(new_cr, uid, context)
                ari_client = None
                # Re-open session object with new_cr
                session = self.sessions.search([
                                ('state', 'in', ['running', 'paused'])],
                                order='create_date desc', limit=1)
                
                try:
                    ari_client = ari.connect(ari_url, ari_user, ari_pass)
                    if self.dialer_type == 'playback':
                        stasis_app_ready.wait(5)                        
                        if not stasis_app_ready.is_set():
                            # Stasis app was not initialized
                            self.env['asterisk.dialer'].browse([self.id]).message_post(
                                'Cannot connect to %s with %s:%s' % (ari_url, ari_user, ari_pass))
                            self.env.cr.commit()
                            session.state = 'cancelled'
                            self.env.cr.commit()
                            return
                
                    # Initial originate should not block
                    go_next_call.set()
                
                    while True:
                        # Sleep 
                        go_next_call.wait(DIALER_RUN_SLEEP)
                        go_next_call.clear()
                        # avoid TransactionRollbackError            
                        self.env.cr.commit()
                        # Clear cash on every round as data could be updated from controller
                        self.env.invalidate_all()
                    
                        stop_run = False
                        
                        if  self.cancel_request:
                            session.state = 'cancelled'
                            session.cancel_request = False
                            stop_run = True
                        
                        elif self.pause_request:
                            session.state = 'paused'
                            session.pause_request = False
                            stop_run = True
                    
                        if stop_run:
                            try:
                                ari_client.events.userEvent(eventName='exit_request',
                                    application='dialer-%s-session-%s' % (
                                            self.id, session.id))
                            except HTTPError:
                                pass
                            return
                                            
                        # Go next round    
                        session_queue = session.queue.search([
                                                    ('state','=','queued'),
                                                    ('session', '=', session.id)
                                                    ], limit=self.simult)
                    
                        if not session_queue and self.env['asterisk.dialer.channel'].search_count(
                                        [('dialer','=',self.id)]) == 0:
                            # All done as queue is empty and no active channels
                            session.state = 'done'
                            self.env.cr.commit()
                            return
                        
                        # Check if we can add more calls
                        self.env.cr.commit()
                        if len(self.channels) >= self.simult:
                            #print 'NO AVAILABLE CHANNELS, TRY NEXT TIME'
                            continue
            
                        for contact in session_queue:                            
                            contact.state = 'process'
                            self.env.cr.commit()
                            originate_call(contact)
                
                except Exception, e:
                    # on client.close() we are always here :-) So just ignore it.
                    if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args[0] == 104: 
                        print e
                        pass
                    else:
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        e_txt = '<br/>'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                        # Had to use old api new api compains here about closed cursor :-(
                        dialer_obj = self.pool.get('asterisk.dialer')
                        dialer = dialer_obj.browse(new_cr, self.env.uid, [self.id]).message_post(e_txt)
                        new_cr.commit()
                        print e_txt
                        try:
                            client.close()
                        except:
                            pass
                    
                
                finally:                   
                    self.env.cr.commit()
                    
                    if self.dialer_type == 'playback':
                        try:
                            ari_client.events.userEvent(eventName='exit_request',
                                    application='dialer-%s-session-%s' % (
                                            self.id, self.active_session.id))
                        except HTTPError:
                            pass
                        except Exception,e:
                            raise
                    self.env.cr.close()
                    
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
    def reset(self):
        if self.active_session:            
            self.active_session.state = 'cancelled'
            self.env.cr.commit()
        
    
    @api.one
    def cancel(self):
        self.active_session.cancel_request = True
        self.env.cr.commit()
    
    
    @api.one
    def pause(self):
        if self.active_session.state == 'running':
            self.active_session.pause_request = True
            self.env.cr.commit()

    @api.one
    def resume(self):
        if self.active_session and self.active_session.state == 'paused':
            self.active_session.state = 'running'
            self.env.cr.commit()
            self.start()



SESSION_STATE_CHOICES = (
    ('running', _('Running')),
    ('done', _('Done')),
    ('cancelled', _('Cancelled')),
    ('paused', _('Paused')),
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
    
    dialer = fields.Many2one('asterisk.dialer', string=_('Dialer'))
    queue = fields.One2many('asterisk.dialer.queue', 'session')
    state = fields.Selection(SESSION_STATE_CHOICES, string=_('State'))
    progress = fields.Integer(compute='_get_progress', string=_('Progress'))
    total = fields.Integer(string=_('Total'))
    sent = fields.Integer(string=_('Sent'))
    answer = fields.Integer(string=_('Answered'))
    busy = fields.Integer(string=_('Busy'))
    congestion = fields.Integer(string=_('Congestion'))
    noanswer = fields.Integer(string=_('No answer'))
    chanunavail = fields.Integer(string=_('Chanunavail'))
    cancel_request = fields.Boolean()
    pause_request = fields.Boolean()
    
    @api.one
    def _get_progress(self):
        self.progress = float(self.sent)/self.total*100
        
    _defaults = {
        'state': 'running',
        'total': 0,
        'sent': 0,
        'answer': 0,
        'busy': 0,
        'congestion': 0,
        'noanswer': 0,
        'chanunavail': 0,
        'cancel_request': False,
        'pause_request': False,
    }
        
    
    
QUEUE_STATE_CHOICES = (
    ('queued', _('Queued')),
    ('process',_('Process')),
    ('done', _('Done')),
)
class queue(models.Model):
    """
    This model holds dialer session calls queue e.g. phone phones to be dialed.
    phones are added to the queue from dialer contacts when session new dialing 
    session is opened.
    """
    _name = 'asterisk.dialer.queue'
    
    session = fields.Many2one('asterisk.dialer.session', string=_('Session'))
    phone = fields.Char(string=_('Phone'))
    name = fields.Char(string=_('Name'))
    state = fields.Selection(QUEUE_STATE_CHOICES, string=_('State'))
    
    _defaults = {
        'state': 'queued',
    }

 
 
class channel(models.Model):
    _name = 'asterisk.dialer.channel'
    
    dialer = fields.Many2one('asterisk.dialer', ondelete='cascade', string=_('Dialer'))
    session = fields.Many2one('asterisk.dialer.session', string=_('Session'))
    channel_id = fields.Char(select=1)
    other_channel_id = fields.Char(select=1)
    name = fields.Char(string=_('Name'))
    phone = fields.Char(string=_('Phone'))
    start_time = fields.Datetime(string=_('Call started'))
 


CDR_CHOICES = (
    ('PROGRESS', _('Progress')),
    ('ANSWER', _('Answer')),
    ('BUSY', _('Busy')),
    ('CONGESTION', _('Congestion')),
    ('NOANSWER', _('No answer')),
    ('CHANUNAVAIL', _('Channel unavailable')),
    ('CANCEL', _('Cancel')),
)



class cdr(models.Model):
    _name = 'asterisk.dialer.cdr'
    
    dialer = fields.Many2one('asterisk.dialer', ondelete='cascade', string=_('Dialer'))
    channel_id = fields.Char(select=1)
    other_channel_id = fields.Char(select=1)
    name = fields.Char(string=_('Name'), select=1)
    phone = fields.Char(string=_('Phone'), select=1)
    status = fields.Selection(CDR_CHOICES, select=1, string=_('Status'))
    start_time = fields.Datetime(string=_('Started'), select=1)
    end_time = fields.Datetime(string=_('Ended'), select=1)
    answered_time = fields.Integer(string=_('Answer seconds'))
    answered_time_str = fields.Char(compute='_get_answered_time_str', string=_('Answer time'))
    playback_start_time = fields.Datetime(string=_('Playback started'))
    playback_end_time = fields.Datetime(string=_('Playback ended'))
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
        

class subscriber_list(models.Model):
    _name = 'asterisk.dialer.subscriber.list'

    name = fields.Char(required=True, string=_('Name'))
    subscriber_count = fields.Integer(compute='_subscriber_count', string=_('Phone of subscribers'))
    
    @api.one
    def _subscriber_count(self):
        if not self.id:
            self.subscriber_count = 0
        else:
            self.subscriber_count = self.env['asterisk.dialer.subscriber'].search_count([('subscriber_list.id', '=', self.id)])#('subscriber_list.id', '=', self.id)])


class subscriber(models.Model):
    _name = 'asterisk.dialer.subscriber'
    _order = 'name, phone'
    
    subscriber_list = fields.Many2one('asterisk.dialer.subscriber.list', 
        required=True, ondelete='cascade')
    name = fields.Char(string=_('Subscriber name'), required=True) 
    phone = fields.Char(string=_('Phone'), required=True)
    
    @api.model
    def _get_latest_list(self):
        latest = self.env['asterisk.dialer.subscriber.list'].search([], limit=1, order='id desc')
        return latest if latest else False


    _defaults = {
        'subscriber_list': _get_latest_list,
    }
    
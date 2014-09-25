import ari
import datetime
import time
import threading
import uuid
from openerp import fields, models, api, sql_db, _
from openerp.osv.osv import except_osv


#DIALER_TYPE_CHOICES = (    
#    ('ondemand', _('On demand')),
#)
    

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
    #dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'))
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
        #'dialer_type': 'ondemand',
        'dialer_model': 'res.partner',
        'state': 'draft',
        'from_time': 10.00,
        'to_time': 18.00,
        'simult': 1,
    }
    
    @api.one
    def start(self):
        
        if not self.dialer_domain:
            raise except_osv(_('Warning'), _('You have nobody to dial. Add contacts first :-)'))
        
        
        if not self.active_session:
            domain = [('phone', '!=', None)] + [eval(self.dialer_domain)[0]]
            contacts = self.env[self.dialer_model].search(domain)
            session = self.env['asterisk.dialer.session'].create({
                'dialer': self.id,
                'total': len(contacts),
            })
            print 'CREATED SESSION', session
            for contact in contacts:
                queue = self.env['asterisk.dialer.queue'].create({
                    'session': session.id,
                    'phone': contact.phone,
                    'name': contact.name,
                })
        else:            
            session = self.active_session
            print 'REUSING SESSION', session
        
        
        stasis_app_ready = threading.Event()
        go_next_call = threading.Event()
        stasis_eneded = threading.Event()
        last_call = threading.Event()
        
        def run_stasis_app():
            
            def stasis_start(channel, ev):
                pass
                
                
            def call_not_connected():
                pass
                
                
            def application_replaced(app):
                pass
                
                
            def user_event(channel, ev):
                if ev['eventname'] == 'go_next_call':
                    print 'PUSHING FOR NEXT CALL'
                    go_next_call.set()
                    
                elif ev['eventname'] == 'exit_request':
                    client.close()
                    
            
            def hangup_request(channel, ev):
                # Update current calls                
                dialer_channel = self.env['asterisk.dialer.channel'].search(
                                    [('channel_id', '=', channel.json.get('id'))])
                if dialer_channel:
                    dialer_channel.unlink()
                    self.env.cr.commit() 
                # Update cdr                
                cdr = self.env['asterisk.dialer.cdr'].search([('channel_id','=','%s' % channel.json.get('id'))])
                if cdr:
                    cdr.write({'status': 'ANSWER', 'end_time': datetime.datetime.now()})
                    self.env.cr.commit()
                if last_call.is_set():
                    # This is the last call, exit.
                    print 'WOW, LAST CALL. CLOSING.'
                    client.close()
                # Awake to go next call
                go_next_call.set()
                
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()
            uid, context = self.env.uid, self.env.context
            with api.Environment.manage():
                self.env = api.Environment(new_cr, uid, context)

                try:
                    client = ari.connect('http://localhost:8088', 'dialer', 'test')
                    client.on_channel_event('StasisStart', stasis_start)
                    client.on_event('ApplicationReplaced', application_replaced)
                    client.on_channel_event('ChannelUserevent', user_event)
                    client.on_channel_event('ChannelHangupRequest', hangup_request)
                    stasis_app_ready.set()
                    client.run(apps='odoo-dialer-%s' % self.id)
                except Exception, e:
                    # on client.close()
                    if e.args[0] == 104: 
                        pass
                    else:
                        print 'Exception', e
                        try:
                            client.close()
                        except:
                            pass
                    # Terminate thread run and let run dialer know about it.
                    stasis_eneded.set()
                    return
                
                finally:
                    self.env.cr.close()
 
            
            
        def run_dialer():
            
            def originate_call(contact):
                # Generate channel ids
                chan_id = uuid.uuid1()
                channelId = '%s-1' % chan_id
                otherChannelId = '%s-2' % chan_id
            
                # ARI originate
                client = ari.connect('http://localhost:8088', 'dialer', 'test')
                ari_channel = client.channels.originate(
                    endpoint='Local/%s@dialer' % contact.phone,
                    app='odoo-dialer-%s' % self.id,
                    channelId=channelId,
                    otherChannelId=otherChannelId)
                
                # +1 current call
                channel = self.env['asterisk.dialer.channel'].create({
                    'dialer': self.id,
                    'channel_id': channelId,
                    'other_channel_id': otherChannelId,
                    'phone': contact.phone,
                    'name': contact.name})
                self.env.cr.commit()
                    
            
            uid, context = self.env.uid, self.env.context
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()  
            with api.Environment.manage():                                  
                self.env = api.Environment(new_cr, uid, context)
                stasis_app_ready.wait(5)
                if not stasis_app_ready.is_set():
                    # Could not connect to ARI
                    raise Exception('Cannot connect to ARI')
                print 'DIALER THREAD STARTED', time.time()
                
                # Initial originate should not block
                go_next_call.set()
                
                while True:
                    go_next_call.wait(3)
                    go_next_call.clear()
                    
                    #self.invalidate_cache()
                    #self.active_session.invalidate_cache()                    
                    self.env.invalidate_all()
                    print 'CHECK CANCEL REQ', self.cancel_request
                    print 'CHECK PAUSE REQ', self.pause_request
                    
                    stop_run = False
                    if stasis_eneded.is_set():
                        self.session.sate = 'done'
                    elif  self.cancel_request:
                        self.active_session.state = 'cancelled'
                        self.active_session.cancel_request = False
                        stop_run = True
                    elif self.pause_request:
                        self.active_session.state = 'paused'
                        self.active_session.pause_request = False
                        stop_run = True
                    if stop_run:
                        self.env.cr.commit()
                        print 'STOP RUN'
                        return
                        
                    
                    # Go next round    
                    session_queue = self.active_session.queue.search([
                            ('state','=','queued'),
                            ('session', '=', self.active_session.id),
                        ], 
                        limit=self.simult)
                    
                    if not session_queue:
                        # All done
                        self.active_session.state = 'done'
                        self.env.cr.commit()
                        last_call.set()
                        print 'ALL DONE, DIALER RUN EXIT.'
                        return
                        
                    # Check if we can add more calls
                    self.env.cr.commit()
                    #channels = self.env['asterisk.dialer.channel'].search([('dialer','=',self.id)])
                    print 'CURRENT CHANNELS', len(self.channels)
                    if len(self.channels) >= self.simult:
                        print 'NO AVAILABLE CHANNELS, TRY NEXT TIME'
                        continue
            
                    for contact in session_queue:
                        print 'MAKEING CONTACT STATE'
                        contact.state = 'process'
                        self.env.cr.commit()
                        new_cr.commit()
                        print 'CONTACT STATE', contact, contact.state
                        originate_call(contact)
                                        
                    print 'GOING NEXT ROUND'
                
                # TODO: put it in finally
                self.env.cr.close()
                
        
        stasis_app = threading.Thread(target=run_stasis_app, name='Stasis app thread')
        stasis_app.start()
        dialer_worker = threading.Thread(target=run_dialer, name='Run dialer thread')
        dialer_worker.start()   
        return {'type': 'ir.actions.act_window_close'}
 
        
    @api.one
    def reset(self):
        if self.active_session:
            self.active_session.state = 'cancelled'
        
    @api.one
    def cancel(self):
        self.active_session.cancel_request = True
            
        #client = ari.connect('http://localhost:8088', 'dialer', 'test')
        #try:
        #    client.events.userEvent(eventName='cancel_request', application='odoo-dialer-%s' % self.id)
        #except Exception, e:
        #    if e.args[0] == '404 Client Error: Not Found':
        #        pass
        #    else:
        #        raise

    @api.one
    def pause(self):
        self.active_session.pause_request = True


    @api.one
    def resume(self):
        if self.active_session and self.active_session.state == 'paused':
            self.active_session.state = 'running'
        self.start()
    
        
    @api.model
    def run_dialer(self):
        context = self.env.context
        uid = self.env.uid
        cr = sql_db.db_connect(self.env.cr.dbname).cursor()
        with api.Environment.manage():
            self.env = env = api.Environment(cr, uid, context)
            dialer = self
            cr.commit()
            
            cr.commit()            
           
            # Get possible call load based on simult restriction
            channel_count = env['asterisk.dialer.channel'].search_count([('dialer', '=', dialer.id)])
            cr.commit()
            
            cr.commit()
            call_limit = dialer.simult - channel_count
            for contact in contacts:
                # Check cancel request:
                if dialer.cancel_request:
                    pass
                print 'DOING CONTACT', contact
                # Generate channel ids
                
                
                print 'CREATED CHANNEL', channel
                env.cr.commit()

                # Create cdr
                cdr = env['asterisk.dialer.cdr'].create({
                    'dialer': dialer.id,
                    'channel_id': channelId,
                    'other_channel_id': otherChannelId,
                    'phone': contact.phone,
                    'name': contact.name,
                    'status': 'PROGRESS',
                    'start_time': datetime.datetime.now(),
                    })
                print 'CREATED CDR', cdr
                env.cr.commit()
            
            print 'SETTING DONE'
            dialer.state = 'done'
            print 'STATE', dialer['state']
            env.cr.commit()
            env.cr.close()
            client.events.userEvent(eventName='exit_request', application='odoo-dialer-%s' % self.id)
            print 'SENT exit_request to Stasis app'


    @api.model
    def run_stasis_app(self):
        context = self.env.context
        uid = self.env.uid
        cr = sql_db.db_connect(self.env.cr.dbname).cursor()
        exit_request = False
        with api.Environment.manage():
            self.env = env = api.Environment(cr, uid, context)
            
            def playback_started(playback, ev):
                # Update playback start time
                channel_id = ev['playback']['target_uri'].split(':')[1]
                cdr = env['asterisk.dialer.cdr'].search([('channel_id','=','%s' % channel_id)])
                cr.commit()
                if cdr:
                    cdr.write({'playback_start_time': datetime.datetime.now()})
                    cr.commit()

            def playback_finished(playback, ev):
                # Update playback_end_time
                channel_id = ev['playback']['target_uri'].split(':')[1]
                cdr = env['asterisk.dialer.cdr'].search([('channel_id','=','%s' % channel_id)])
                cr.commit()
                if cdr:
                    cdr.playback_end_time = datetime.datetime.now()
                    cr.commit()
                # Hangup now!
                client.channels.get(channelId=channel_id).hangup()
            
               
            def stasis_start(channel, ev):
                channel.answer()
                play_file = 'demo-thanks'
                channel.play(media='sound:%s' % play_file)
                

            def stasis_end(channel, ev):
                print "%s has left the application" % channel.json.get('name')
                # Update current calls                
                dialer_channel = env['asterisk.dialer.channel'].search(
                                    [('channel_id', '=', channel.json.get('id'))])
                cr.commit()
                if dialer_channel:
                    print 'Removing channel', dialer_channel['channel_id']
                    dialer_channel.unlink()
                    cr.commit() 
                # Update cdr                
                cdr = env['asterisk.dialer.cdr'].search([('channel_id','=','%s' % channel.json.get('id'))])
                cr.commit()
                if cdr:
                    cdr.write({'status': 'ANSWER', 'end_time': datetime.datetime.now()})
                    cr.commit()
                if exit_request:
                    # This is the last call, exit.
                    client.close()
                    cr.close()
                    
                    
            def hangup_request(channel, ev):
                print 'CHANNEL HANGUP REQUEST, DIALER ID', self.id

            def user_event(channel, ev):
                if ev['eventname'] == 'exit_request':
                    print 'ARI exit request for dialer id: %s' % self.id
                    exit_request = True
                    

            client = ari.connect('http://localhost:8088', 'dialer', 'test')
            client.on_channel_event('StasisStart', stasis_start)
            client.on_channel_event('StasisEnd', stasis_end)
            client.on_channel_event('ChannelHangupRequest', hangup_request)
            client.on_channel_event('ChannelUserevent', user_event)
            client.on_playback_event('PlaybackStarted', playback_started)
            client.on_playback_event('PlaybackFinished', playback_finished)

            try:
                client.run(apps='odoo-dialer-%s' % self.id)
            except Exception, e:
                if e.args[0] == 104: # on client.close()
                    pass
                else:
                    raise
 



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
    answered = fields.Integer(string=_('Answered'))
    busy = fields.Integer(string=_('Busy'))
    congestion = fields.Integer(string=_('Congestion'))
    no_answer = fields.Integer(string=_('No answer'))
    failed = fields.Integer(string=_('Failed'))
    cancel_request = fields.Boolean()
    pause_request = fields.Boolean()

    
    @api.one
    def _get_progress(self):
        self.progress = float(self.sent)/self.total*100
        
    _defaults = {
        'state': 'running',
        'total': 0,
        'sent': 0,
        'answered': 0,
        'busy': 0,
        'congestion': 0,
        'no_answer': 0,
        'failed': 0,
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
    #duration = fields.Integer(compute='_get_duration', string=_('Duration'))
    duration_str = fields.Char(compute='_get_duration_str', string=_('Call duration'))
    playback_start_time = fields.Datetime(string=_('Playback started'))
    playback_end_time = fields.Datetime(string=_('Playback ended'))
    #playback_duration = fields.Integer(compute='_get_playback_duration', string=_('Duration'))
    playback_duration_str = fields.Char(compute='_get_playback_duration_str', string=_('Play duration'))
    #answered_time = fields.Integer(string=_('Answered seconds'))
    
    """
    @api.one
    def _get_duration(self):
        if not (self.start_time and self.end_time):
            self.duration = 0
        else:
            self.duration = self.end_time - self.start_time
    """
    
    @api.one
    def _get_duration_str(self):
        # Get nice 00:00:03 string
        if not (self.start_time and self.end_time):
            self.duration_str = ''
        else:
            start_time = datetime.datetime.strptime(self.start_time, '%Y-%m-%d %H:%M:%S')
            end_time = datetime.datetime.strptime(self.end_time, '%Y-%m-%d %H:%M:%S')
            delta = end_time-start_time
            self.duration_str = datetime.timedelta(seconds=delta.seconds).__str__()
    
    
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
    
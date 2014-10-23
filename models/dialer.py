# -*- coding: utf-8 -*-
import ari
import datetime
import logging
import time
import threading
import os, sys, traceback
import uuid
from openerp import fields, models, api, sql_db, _
from openerp.exceptions import ValidationError, DeferredException
from requests.exceptions import HTTPError

_logger = logging.getLogger(__name__)

DIALER_RUN_SLEEP = 2 # Dialer threads sleeps


DIALER_TYPE_CHOICES = (    
    ('playback', _('Playback message')),
    ('dialplan', _('Asterisk dialplan')),
)
    

class dialer(models.Model):
    _name = 'asterisk.dialer'
    _inherit = 'mail.thread'
    _description = 'Asterisk Dialer'
    _order = 'name'
    
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
        """Get open session"""
        session = self.sessions.search([
            ('dialer','=', self.id)],
            order='create_date desc', limit=1)
        self.active_session = session


    name = fields.Char(required=True, string=_('Name'))
    description = fields.Text(string=_('Description'))
    dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'))
    context_name = fields.Char(string=_('Context name'), default='')
    state = fields.Html(compute='_get_state', string=_('State'))
    active_session = fields.Many2one('asterisk.dialer.session', compute='_get_active_session')
    active_session_state = fields.Selection(related='active_session.state')
    sessions = fields.One2many('asterisk.dialer.session', 'dialer')
    sound_file = fields.Many2one('asterisk.dialer.soundfile', string=_('Sound file'), 
        ondelete='restrict')
    #start_time = fields.Datetime(string=_('Start time'), 
    #    help=_('Exact date and time to start dialing. For scheduled dialers.'))
    from_time = fields.Float(digits=(2, 2), string=_('From time'), 
        help=_('Time permitted for calling If dialer is paused it will be resumed this time.')) 
    to_time = fields.Float(digits=(2, 2), string=_('To time'), 
        help=_('Time perimitted for calling. If dialer is running it will be paused this time')) 
    dialer_model = fields.Selection('_get_dialer_model', required=True, string=_('Dialer model'))
    dialer_domain = fields.Char(string=_('Selection'))    
    channels = fields.One2many('asterisk.dialer.channel', 'dialer', string=_('Current calls'))
    cdrs = fields.One2many('asterisk.dialer.cdr', 'dialer', string=_('Call Detail Records'))
    cdr_count = fields.Integer(compute='_get_cdr_count', string=_('Number of call detail records'))
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
        if not self.dialer_domain:
            raise ValidationError(_('You have nobody to dial. Add contacts first :-)'))
        elif self.dialer_type == 'playback' and not self.sound_file:
            raise ValidationError(_('Dialer type is Playback and sound file not set!'))
        elif self.dialer_type == 'dialplan' and not self.context_name:
            raise ValidationError(_('Dialer type is Dialplan and Asterisk context not set!'))
        
        self.env.cr.commit()
        self.env.cr.autocommit(True)        
        server = self.env['asterisk.server.settings'].browse([1])
        dialer_context = server.context_name
        # Get rid of unicode as ari-py does not handle it.
        ari_user = str(server.ari_user)
        ari_pass = str(server.ari_pass)
        ari_url = str('http://' + server.ip_addr + ':' + server.http_port)
        
        # Get active session
        session = self.sessions.search([
                                ('state', 'in', ['running', 'paused'])],
                                order='create_date desc', limit=1)
                                
        if not session:
            domain = [eval(self.dialer_domain)[0]]
            if self.dialer_model == 'res.partner':
                contacts = self.env[self.dialer_model].search(domain + [('phone', '!=', None)])
            else:
                s_lists = self.env[self.dialer_model].search(domain)
                contacts = self.env['asterisk.dialer.subscriber'].search([('subscriber_list','in', [s.id for s in s_lists])])
                
            session = self.env['asterisk.dialer.session'].create({
                'dialer': self.id,
                'total': len(contacts),
            })
            self.env.cr.commit()
            # Create initial CDRs for future calls
            for contact in contacts:
                cdr = self.env['asterisk.dialer.cdr'].create({
                    'dialer': self.id,
                    'session': session.id,
                    'phone': contact.phone,
                    'name': contact.name,
                    'status': 'queue',
                })
                self.env.cr.commit()
        
        sound_file = ''
        if self.dialer_type == 'playback':
            # Get sound file without extension
            sound_file = os.path.splitext(self.sound_file.get_full_path()[0])[0]
        
        self.env.cr.commit()
        
        stasis_app_ready = threading.Event()
        go_next_call = threading.Event()
        
                
        def run_stasis_app():
            
            def answer_channel(channel):
                
                def playback_finished(playback, event):
                    def hangup():
                        channel.hangup()
                    t = threading.Timer(1, hangup)
                    t.start()
                    
                channel.answer()
                playback_id = str(uuid.uuid4())
                playback = channel.play(channelId=channel.id, media='sound:%s' % sound_file)
                playback.on_event('PlaybackFinished', playback_finished)
                
            
            def stasis_start(channel, ev):
                t = threading.Timer(1, answer_channel, [channel])
                t.start()
                
                
                
            def application_replaced(app):
                pass


            def user_event(channel, ev):
                _logger.debug('STASIS: EXIT REQUEST RECEIVED')
                if ev['eventname'] == 'exit_request':
                    client.close()
                    
            
            def hangup_request(channel, ev):
                pass
                                
                
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()
            new_cr.autocommit(True)
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
                    if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args and e.args[0] == 104: 
                        pass
                    else:
                        # Mark Stasis app as not ok
                        stasis_app_ready.clear()
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        e_txt = '<br/>'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                        # Had to use old api new api compains here about closed cursor :-(
                        dialer_obj = self.pool.get('asterisk.dialer')
                        dialer = dialer_obj.browse(new_cr, self.env.uid, [self.id]).message_post('Error:\n%s' % e_txt)
                        new_cr.commit()
                        _logger.debug(e_txt)
                        try:
                            client.close()
                        except:
                            pass

                
                finally:
                    _logger.debug('STASIS APP FINALLY')
                    # If an error happens in Stasis app thread 
                    # let Dialer run thread about it so that it could exit
                    new_cr.commit()
                    new_cr.close()
 
            
            
        def run_dialer():
            
            def originate_call(contact):
                _logger.debug('DIALER: ORIGINATE CALL FOR %s' % contact.phone)
                # Generate channel ids
                chan_id = uuid.uuid1()
                channelId = '%s-1' % chan_id
                otherChannelId = '%s-2' % chan_id

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
                                
                # Update CDR
                cdr = self.env['asterisk.dialer.cdr'].search(
                    [('dialer.id','=', self.id),
                     ('session.id','=',session.id),
                     ('phone','=', contact.phone),
                     ('name','=', contact.name),
                    ])
                if cdr:
                    cdr.write({
                        'channel_id': channelId,
                        'other_channel_id': otherChannelId,
                        'status': 'process',
                        'start_time': datetime.datetime.now(),
                    })
                self.env.cr.commit()
            
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

            uid, context = self.env.uid, self.env.context
            new_cr = sql_db.db_connect(self.env.cr.dbname).cursor()
            new_cr.autocommit(True)
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
                            session.state = 'error'
                            self.env.cr.commit()
                            return
                
                    # Initial originate should not block
                    go_next_call.set()
                
                    while True:
                        # Sleep 
                        print 'GO NEXT CALL BEFORE SLEEP'
                        go_next_call.wait(DIALER_RUN_SLEEP)
                        print 'GO NEXT CALL AFTER SLEEP'
                        
                        if self.dialer_type == 'playback' and not stasis_app_ready.is_set():
                            raise Exception('Stasis App Error.')
                        
                        go_next_call.clear()
                        # avoid TransactionRollbackError            
                        self.env.cr.commit()
                        # Clear cash on every round as data could be updated from controller
                        self.env.invalidate_all()
                        
                        if  self.cancel_request or self.pause_request:
                            if self.cancel_request:
                                session.state = 'cancelled'
                                session.cancel_request = False
                            else:
                                session.state = 'paused'
                                session.pause_request = False
                            
                            _logger.debug('DIALER: CANCEL/PAUSE REQUEST')
                            try:
                                ari_client.events.userEvent(eventName='exit_request',
                                    application='dialer-%s-session-%s' % (
                                            self.id, session.id))
                            except HTTPError:
                                pass
                            
                            self.env.cr.commit()
                            return
                                            
                        # Go next round    
                        current_channels = self.channels.search_count([('session','=',session.id)])
                        cdr_queue = session.cdrs.search([
                                                    ('status','=','queue'),
                                                    ('session', '=', session.id)
                                                    ], limit=self.simult)
                    
                        if not cdr_queue and current_channels == 0:
                            # All done as queue is empty and no active channels
                            _logger.debug('DIALER: QUEUE IS EMPTY')
                            session.state = 'done'
                            self.env.cr.commit()
                            return
                        
                        # Check if we can add more calls
                        self.env.cr.commit()
                        if current_channels >= self.simult:
                            _logger.debug('DIALER: NO AVAIL CHANNELS, SLEEPING')
                            #print 'NO AVAILABLE CHANNELS, TRY NEXT TIME'
                            continue
                        
                        # update sent call
                        session.sent = session.sent + len(cdr_queue)
                        self.env.cr.commit()
                        available_channels = self.simult - current_channels
                        for cdr in cdr_queue[:available_channels]:                            
                            originate_call(cdr)
                
                except Exception, e:
                    # on client.close() we are always here :-) So just ignore it.
                    if hasattr(e, 'args') and type(e.args) in (list, tuple) and e.args[0] == 104: 
                        _logger.debug('DIALER: ARI CLIENT CLOSE')
                        pass
                    else:                        
                        exc_type, exc_value, exc_traceback = sys.exc_info()
                        e_txt = '<br/>'.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                        # Had to use old api new api compains here about closed cursor :-(
                        dialer_obj = self.pool.get('asterisk.dialer')
                        dialer = dialer_obj.browse(new_cr, self.env.uid, [self.id])
                        dialer.active_session.state = 'error'
                        new_cr.commit()
                        dialer.message_post(e_txt)
                        _logger.debug(e_txt)
                        try:
                            client.close()
                        except:
                            pass
                    
                
                finally:
                    _logger.debug('DIALER RUN FINALLY')
                    self.env.cr.commit()
                    
                    if self.dialer_type == 'playback':
                        try:
                            ari_client and ari_client.events.userEvent(eventName='exit_request',
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
        track_visibility='onchange')
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
        

class subscriber_list(models.Model):
    _name = 'asterisk.dialer.subscriber.list'

    name = fields.Char(required=True, string=_('Name'))
    subscriber_count = fields.Integer(compute='_subscriber_count', string=_('Subscribers count'))
    subscribers = fields.One2many('asterisk.dialer.subscriber', 'subscriber_list')
    
    @api.one
    def _subscriber_count(self):
        if not self.id:
            self.subscriber_count = 0
        else:
            self.subscriber_count = self.env['asterisk.dialer.subscriber'].search_count([('subscriber_list.id', '=', self.id)])


class subscriber(models.Model):
    _name = 'asterisk.dialer.subscriber'
    _order = 'name, phone'
    
    subscriber_list = fields.Many2one('asterisk.dialer.subscriber.list', 
        required=True, ondelete='cascade')
    phone = fields.Char(string=_('Phone number'), required=True)
    name = fields.Char(string=_('Subscriber name'))     
    
    @api.model
    def _get_latest_list(self):
        latest = self.env['asterisk.dialer.subscriber.list'].search([], limit=1, order='id desc')
        return latest if latest else False


    _defaults = {
        'subscriber_list': _get_latest_list,
    }
    
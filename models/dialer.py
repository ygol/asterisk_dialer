import ari
import datetime
import time
import threading
import uuid
from openerp import fields, models, api, _
from openerp.api import Environment



DIALER_STATE_CHOICES = (
    ('draft', _('Draft')),
    ('running', _('Running')),
    ('paused', _('Paused')),
    ('time_paused', _('Time paused')),
    ('cancelled', _('Cancelled')),
    ('done', _('Done')),
)

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
        
    name = fields.Char(required=True, string=_('Name'))
    description = fields.Text(string=_('Description'))
    #dialer_type = fields.Selection(DIALER_TYPE_CHOICES, string=_('Type'))
    state = fields.Selection(DIALER_STATE_CHOICES, string=_('State'), track_visibility='onchange')
    sound_file = fields.Binary(string=_('Sound file'))
    start_time = fields.Datetime(string=_('Start time'), 
        help=_('Exact date and time to start dialing. For scheduled dialers.'))
    from_time = fields.Float(digits=(2, 2), string=_('From time'), 
        help=_('Time permitted for calling If dialer is paused it will be resumed this time.')) 
    to_time = fields.Float(digits=(2, 2), string=_('To time'), 
        help=_('Time perimitted for calling. If dialer is running it will be paused this time')) 
    dialer_model = fields.Selection('_get_dialer_model', required=True, string=_('Dialer model'))
    dialer_domain = fields.Char(string=_('Domain'))
    subscriber_lists = fields.Many2many('asterisk.dialer.subscriber', 'campaign', string=_('Subscribers')) 
    sent = fields.Integer(string=_('Sent'))
    answered = fields.Integer(string=_('Answered'))
    busy = fields.Integer(string=_('Busy'))
    congestion = fields.Integer(string=_('Congestion'))
    no_answer = fields.Integer(string=_('No answer'))
    failed = fields.Integer(string=_('Failed'))
    channels = fields.One2many('asterisk.dialer.channel', 'dialer', string=_('Current calls'))
    cdrs = fields.One2many('asterisk.dialer.cdr', 'dialer', string=_('Call Detail Records'))
    cdr_count = fields.Integer(compute='_get_cdr_count', string=_('Number of call detail records'))
    simult = fields.Integer(string=_('Simultaneous calls'))
  
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
        self.state = 'running'
        cr, uid, context = self.env.args
        self.dialer_worker = threading.Thread(target=self.run_dialer, args=(cr, uid, [self.id], context))
        self.dialer_worker.start()        
        self.stasis_app = threading.Thread(target=self.run_stasis_app, args=(cr, uid, [self.id], context))
        self.stasis_app.start()

        
    @api.one
    def cancel(self):
        self.state = 'cancelled'
        client = ari.connect('http://localhost:8088', 'dialer', 'test')
        try:
            client.events.userEvent(eventName='exit_request', application='odoo-dialer-%s' % self.id)
        except Exception, e:
            if e.args[0] == '404 Client Error: Not Found':
                pass
            else:
                raise

    @api.one
    def pause(self):
        self.state = 'paused'

    @api.one
    def resume(self):
        self.state = 'running'
    
        
    @api.model
    def run_dialer(self, cr, uid, ids, context=None):
        with Environment.manage():
            cr = self.pool.cursor()
            dialer = self.pool['asterisk.dialer'].browse(cr, uid, ids, context=context)[0]
            domain = [('phone', '!=', None)] + [eval(dialer.dialer_domain)[0]]
            
            client = ari.connect('http://localhost:8088', 'dialer', 'test')
            
            dialer_channel_obj = self.pool['asterisk.dialer.channel']
            cdr_obj = self.pool['asterisk.dialer.cdr']            
            
            cr.commit()
            
            # Get possible call load based on simult restriction
            channel_count = dialer_channel_obj.search_count(cr, uid, [('dialer', '=', dialer.id)], context=context)
            print 'CURRENT', channel_count
            contact_ids = self.pool[dialer.dialer_model].search(cr, uid, domain, context=context)
            contacts = self.pool[dialer.dialer_model].browse(cr, uid, contact_ids, context=context)
            call_limit = dialer.simult - channel_count
            for contact in contacts[:]:
                # Generate channel ids
                chan_id = uuid.uuid1()
                channelId = '%s-1' % chan_id
                otherChannelId = '%s-2' % chan_id
                
                # Originate call
                channel = client.channels.originate(
                    endpoint='Local/%s@dialer' % contact.phone,
                    app='odoo-dialer-%s' % dialer.id,
                    channelId=channelId,
                    otherChannelId=otherChannelId,
                )
                
                # Update current calls
                dialer_channel_obj.create(cr, uid, {
                    'dialer': dialer.id,
                    'channel_id': channelId,
                    'other_channel_id': otherChannelId,
                    'phone': contact.phone,
                    'name': contact.name,
                    },
                    context=context)
                cr.commit()

                # Create cdr
                cdr_obj.create(cr, uid, {
                    'dialer': dialer.id,
                    'channel_id': channelId,
                    'other_channel_id': otherChannelId,
                    'phone': contact.phone,
                    'name': contact.name,
                    'status': 'PROGRESS',
                    },
                    context=context)
                cr.commit()
                
            cr.close()



    @api.model
    def run_stasis_app(self, cr, uid, ids, context=None):
        cr = self.pool.cursor()
        dialer_channel_obj = self.pool['asterisk.dialer.channel']
        cdr_obj = self.pool.get('asterisk.dialer.cdr')
        with Environment.manage():
              
            def playback_started(playback, ev):
                # Update playback start time
                channel_id = ev['playback']['target_uri'].split(':')[1]
                cdr_id = cdr_obj.search(cr, uid, [('channel_id','=','%s' % channel_id)],
                                                                                context=context)
                if cdr_id:
                    cdr = cdr_obj.browse(cr, uid, cdr_id, context=context)
                    cdr.write({'playback_start_time': datetime.datetime.now()})
                    cr.commit()

            def playback_finished(playback, ev):
                # Update playback_end_time
                channel_id = ev['playback']['target_uri'].split(':')[1]
                cdr_id = cdr_obj.search(cr, uid, [('channel_id','=','%s' % channel_id)],
                                                                                context=context)
                if cdr_id:
                    cdr = cdr_obj.browse(cr, uid, cdr_id, context=context)
                    cdr.playback_end_time = datetime.datetime.now()
                    cr.commit()
                # Hangup now!
                client.channels.get(channelId=channel_id).hangup()
            
               
            def stasis_start(channel, ev):
                channel.answer()
                play_file = 'demo-thanks'
                channel.play(media='sound:%s' % play_file)
                # Update cdr                
                cdr_id = cdr_obj.search(cr, uid, [('channel_id','=','%s' % channel.json.get('id'))],
                    context=context)
                if cdr_id:
                    cdr = cdr_obj.browse(cr, uid, cdr_id, context=context)
                    cdr.start_time = datetime.datetime.now()
                    cr.commit()
                

            def stasis_end(channel, ev):
                print "%s has left the application" % channel.json.get('name')
                # Update current calls                
                dialer_channel_id = dialer_channel_obj.search(cr, uid, 
                    [('channel_id', '=', channel.json.get('id'))],
                    context=context)
                if dialer_channel_id:
                    print 'Removing channel', dialer_channel_id
                    dialer_channel_obj.unlink(cr, uid, dialer_channel_id)
                    cr.commit()
                    
                # Update cdr                
                cdr_id = cdr_obj.search(cr, uid, [('channel_id','=','%s' % channel.json.get('id'))],
                                                                                context=context)
                if cdr_id:
                    cdr = cdr_obj.browse(cr, uid, cdr_id, context=context)
                    cdr.write({'status': 'ANSWER', 'end_time': datetime.datetime.now()})
                    cr.commit()
                    
                    
            def hangup_request(channel, ev):
                print 'CHANNEL HANGUP REQUEST, DIALER ID'

            def user_event(channel, ev):
                if ev['eventname'] == 'exit_request':
                    print 'ARI exit request for dialer id: %s' % self.id
                    client.close()

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
        cr.close()
 
 
 
class dialer_channel(models.Model):
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
    subscriber_count = fields.Integer(compute='_subscriber_count', string=_('Number of subscribers'))
    
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
    
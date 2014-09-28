from openerp import http
from openerp.http import request
from openerp import SUPERUSER_ID
from datetime import datetime


class dialer(http.Controller):
    
    @http.route('/dialer/channel_update/', auth='none', type='http')
    def channel_update(self, channel_id, status, answered_time):
        """
        This function is used to track originated calls not connected to Stasis app.
        All unsuccessful calls get here. Answered calls are handled in Stasis app.
        """
        dialer_channel_obj = request.registry.get('asterisk.dialer.channel')
        cdr_obj = request.registry.get('asterisk.dialer.cdr')
        dialer_obj = request.registry('asterisk.dialer')
        dialer_session_obj = request.registry('asterisk.dialer.session')
        dialer = None
        dialer_session = None
        dialer_channel = None
        
        dialer_channel_id = dialer_channel_obj.search(request.cr, SUPERUSER_ID, 
            [('other_channel_id', '=', '%s' % channel_id)],
            context=request.context)
        dialer_channel = dialer_channel_obj.browse(request.cr, SUPERUSER_ID, dialer_channel_id, context=request.context)
        if dialer_channel:
            # Update session, some magic here as we have exact names like DIALSTATUS returns.
            if status.lower() in dialer_channel.session.fields_get_keys():
                request.cr.commit()
                current = dialer_channel.session[status.lower()]
                request.cr.commit()
                dialer_channel.session[status.lower()] = current + 1
                request.cr.commit()

            # Remove channel
            dialer_channel_obj.unlink(request.cr, SUPERUSER_ID, dialer_channel.id, 
                context=request.context)
                
        # Update cdr
        cdr_id = cdr_obj.search(request.cr, SUPERUSER_ID,
                [('other_channel_id','=','%s' % channel_id)],
                context=request.context)
        if cdr_id:
            cdr = cdr_obj.browse(request.cr, SUPERUSER_ID, cdr_id, context=request.context)
            cdr.write({'status': '%s' % status, 
                        'end_time': datetime.now(),
                        'answered_time': answered_time,
                        'state': 'done',
                    })
            request.cr.commit()
            return 'OK'
        else:
            return 'NOT FOUND'
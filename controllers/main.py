from openerp import http
from openerp.http import request
from openerp import SUPERUSER_ID


class dialer(http.Controller):
    
    @http.route('/dialer/channel_update/', auth='none', type='http')
    def channel_update(self, channel_id, status):
        """
        This function is used to track originated calls not connected to Stasis app.
        All unsuccessful calls get here. Answered calls are handled in Stasis app.
        """
        dialer_channel_obj = request.registry.get('asterisk.dialer.channel')
        cdr_obj = request.registry.get('asterisk.dialer.cdr')
        
        # Remove channel from current calls
        dialer_channel_id = dialer_channel_obj.search(request.cr, SUPERUSER_ID, 
            [('other_channel_id', '=', '%s' % channel_id)],
            context=request.context)
        if dialer_channel_id:
            dialer_channel_obj.unlink(request.cr, SUPERUSER_ID, dialer_channel_id, 
                context=request.context)
                
        # Create cdr
        cdr_id = cdr_obj.search(request.cr, SUPERUSER_ID,
                [('other_channel_id','=','%s' % channel_id)],
                context=request.context)
        if cdr_id:
            cdr = cdr_obj.browse(request.cr, SUPERUSER_ID, cdr_id, context=request.context)
            cdr.status = '%s' % status
            request.cr.commit()
            return 'OK'
        else:
            return 'NOT FOUND'
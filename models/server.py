from openerp import fields, models, api, _

class server_settings(models.Model):
    _name = 'asterisk.server.settings'

    ip_addr = fields.Char(required=True, string=_('Asterisk server IP adress, not hostname'),
        default='127.0.0.1')
    http_port = fields.Char(required=True, string=_('Asterisk server HTTP port'),
        default='8088')
    ari_user = fields.Char(required=True, string=_('ARI username'), default='')
    ari_pass = fields.Char(required=True, string=_('ARI password'), default='')
    
    
    @api.one
    def execute(self):
        pass
        
        
    @api.one
    def clear(self):
        self.ari_user = ''
        self.ari_pass = ''
        

    
    

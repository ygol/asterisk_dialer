from openerp import fields, models, api, _

class server_settings(models.Model):
    _name = 'asterisk.server.settings'

    ari_url = fields.Char(required=True, string=_('Server URL'))
    ari_user = fields.Char(required=True, string=_('ARI username'))
    ari_pass = fields.Char(required=True, string=_('ARI password'))
    context_name = fields.Char(required=True, string=_('Dialplan context'))
    

    _defaults = {
        'ari_url': 'http://localhost:8088',
        'context_name': 'dialer',
        'ari_user': '',
        'ari_pass': '',
    }
    
    @api.one
    def execute(self):
        print self.ari_url, self.ari_user
        
        
    @api.one
    def clear(self):
        self.ari_user = ''
        self.ari_pass = ''
        

    
    

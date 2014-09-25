from openerp import fields, models, api, _

class server_settings(models.TransientModel):
    _name = 'asterisk.server.settings'
    _inherit = 'res.config.settings'


    ari_url = fields.Char(required=True, string=_('Server URL'))
    ari_user = fields.Char(required=True, string=_('ARI username'))
    ari_pass = fields.Char(required=True, string=_('ARI password'))
    context_name = fields.Char(required=True, string=_('Dialplan context'))
    

    _defaults = {
        'ari_url': 'http://localhost:8088',
        'context_name': 'dialer',
    }
    


from openerp import fields, models, api, _

class server(models.Model):
    _name = 'asterisk.server'
    
    name = models.Char()
    ari_url = models.Char()
    ari_user = models.Char()
    ari_pass = models.Char()
    context = models.Char()
    extension = models.Char()
    

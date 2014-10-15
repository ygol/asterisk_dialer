import logging
from openerp import fields, models, api, _

_logger = logging.getLogger(__name__)

class SoundFile(models.Model):
    _name = 'asterisk_dialer.sound_file'
    
    name = fields.Char(string=_('Sound file name'), required=True)
    datas = fields.Binary(compute='_data_get', inverse='_data_set', string=_('File content'), required=True)
    datas_fname = fields.Char(string=_('File name'), required=True)
    description = fields.Text(string=_('Description'))
    
    @api.one
    @api.depends('datas_fname')
    def _data_get(self):
        if not  self.datas_fname:
            return
            
        filename = '/tmp/%s' % self.datas_fname
        try:
            r = open(filename, 'rb').read().encode('base64')
            self.datas = r
        except IOError:
            _logger.exception('_data_get reading: %s' % filename)

        
    @api.one
    @api.depends('datas_fname')
    def _data_set(self):
        bin_value = self.datas.decode('base64')
        filename = '/tmp/%s' % self.datas_fname
        try:
            with open(filename, 'wb') as fp:
                fp.write(bin_value)
        except IOError:
            _logger.exception('_data_set writing: %s' % filename)
                    
    
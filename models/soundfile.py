# -*- coding: utf-8 -*-
import commands
import logging
import os
import tempfile
from openerp import fields, models, api, tools, exceptions, _

_logger = logging.getLogger(__name__)

class SoundFile(models.Model):
    _name = 'asterisk.dialer.soundfile'
    
    name = fields.Char(string=_('Sound file name'), required=True)
    datas = fields.Binary(compute='_data_get', inverse='_data_set', string=_('File content'), required=True)
    datas_fname = fields.Char(string=_('File name'), required=True)
    description = fields.Text(string=_('Description'))
    version = fields.Integer()
    
    
    @api.one
    def get_full_path(self):
        sound_dir = os.path.join(tools.config.filestore(self.env.cr.dbname), 'sounds')
        if not os.path.isdir(sound_dir):
            os.mkdir(sound_dir)
        filename = os.path.join(sound_dir, self.datas_fname)
        return filename.encode('utf-8')

    
    @api.one
    def _data_get(self):
        if not  self.datas_fname:
            self.datas = ''
            return            
        filename = self.get_full_path()[0]
        try:
            r = open(filename, 'rb').read().encode('base64')
            self.datas = r
        except IOError:
            self.datas = ''
            _logger.exception('_data_get reading: %s' % filename)

        
    @api.one
    def _data_set(self):        
        filename = self.get_full_path()[0]
        tmp_fd, tmp_filename =  tempfile.mkstemp()
        try:
                bin_value = self.datas.decode('base64')
                tmp_file = open(tmp_filename,'wb')
                tmp_file.write(bin_value)
                tmp_file.close()
                os.close(tmp_fd)
                sox_status, sox_output = commands.getstatusoutput('sox "%s" -c 1 -r 8000 "%s"' % (tmp_filename, filename))
                if not sox_status == 0:
                    _logger.debug('sox error: %s' % sox_output)
                    raise exceptions.Warning(_('File conversion error. Cannot convert to WAVE audio, Microsoft PCM, 16 bit, mono 8000 Hz'))                
        
        except IOError:
            _logger.exception('_data_set writing: %s' % filename)
            
        finally:
            if os.path.exists(tmp_filename):
                os.unlink(tmp_filename)
            

    @api.one
    def unlink(self):        
        try:
            # Delete only if this is last file
            if self.env['asterisk.dialer.soundfile'].search_count([('datas_fname','=',self.datas_fname)]) == 1:
                os.unlink(self.get_full_path()[0])
        except:
            pass
        super(SoundFile, self).unlink()
        
        
    @api.one
    def write(self, vals):
        if not self.datas_fname == vals.get('datas_fname'):
            try:
                os.unlink(self.get_full_path()[0])
            except OSError:
                pass
            vals['version'] = self.version + 1
        super(SoundFile, self).write(vals)


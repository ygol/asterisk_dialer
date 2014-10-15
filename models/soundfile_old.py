import logging
import os
from openerp.osv import orm, fields
from openerp import tools, _
from openerp.exceptions import Warning

_logger = logging.getLogger(__name__)

class SoundFile(orm.Model):
    _name = 'asterisk_dialer.sound_file'

    def get_full_path(self, cr, uid, ids, context=None):
        sound_file = self.browse(cr, uid, ids, context=context)
        sound_dir = os.path.join(tools.config.filestore(cr.dbname), 'sounds')
        filename = os.path.join(sound_dir, sound_file.datas_fname)
        return filename


    def _data_get(self, cr, uid, ids, name, arg, context=None):
        result = {}
        for sound_file in self.browse(cr, uid, ids, context=context):
            sound_dir = os.path.join(tools.config.filestore(cr.dbname), 'sounds')
            filename = os.path.join(sound_dir, sound_file.datas_fname)
            try:
                r = open(filename, 'rb').read().encode('base64')
                result[sound_file.id] = r
            except IOError:
                _logger.exception('_data_get reading: %s' % filename)
        return result


    def _data_set(self, cr, uid, ids, name, value, arg, context=None):
        if not  value:
            return True
        bin_value = value.decode('base64')
        sound_file = self.browse(cr, uid, ids, context=context)
        sound_dir = os.path.join(tools.config.filestore(cr.dbname), 'sounds')
        if not os.path.exists(sound_dir):
            os.mkdir(sound_dir)
        filename = os.path.join(sound_dir, sound_file.datas_fname)
        if os.path.exists(filename):
            raise Warning(_('File already exists!'))
        try:
            with open(filename, 'wb') as fp:
                fp.write(bin_value)
        except IOError:
            _logger.exception('_data_set writing: %s' % filename)
        return True



    _columns = {
        'name': fields.char(string=_('Sound name'), required=True),
        'datas': fields.function(fnct=_data_get, fnct_inv=_data_set, type='binary', 
            string=_('File content'), required=True),
        'datas_fname': fields.char(string=_('File name'), required=True),
        'description': fields.text(string=_('Description')),
    }
    
    

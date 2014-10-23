openerp.asterisk_dialer = function(instance) {    
    var _t = instance.web._t,
        _lt = instance.web._lt;
    var QWeb = instance.web.qweb;
    
    instance.web.form.widgets.add('soundfile', 'instance.web.form.FieldSoundFile');
    
    instance.web.form.FieldSoundFile= instance.web.form.FieldBinary.extend({
        template: 'FieldSoundFile',
        initialize_content: function() {
        this._super();
        if (this.get("effective_readonly")) {
            var self = this;
            this.$el.find('a').click(function(ev) {
                if (self.get('value')) {
                    self.on_save_as(ev);
                }
                return false;
            });
        }
    },
    render_value: function() {
        var show_value;
            
        if (!this.get("effective_readonly")) {
            if (this.node.attrs.filename) {
                show_value = this.view.datarecord[this.node.attrs.filename] || '';
            } else {
                show_value = (this.get('value') !== null && this.get('value') !== undefined && this.get('value') !== false) ? this.get('value') : '';
            }
            this.$el.find('input').eq(0).val(show_value);
        } else {
            this.$el.find('a').toggle(!!this.get('value'));
            if (this.get('value')) {
                show_value = _t("Download");
                if (this.view)
                    show_value += " " + (this.view.datarecord[this.node.attrs.filename] || '');
                this.$el.find('a').text(show_value);
            }
            var url;
            url = this.session.url('/web/binary/saveas', {
                                        model: this.view.dataset.model,
                                        id: (this.view.datarecord.id || ''),
                                        field: this.name,
                                        version: this.view.datarecord.version,
                                        filename_field: (this.node.attrs.filename || ''),
                                        });
            var $audio = $(QWeb.render('FieldSoundFile-audio', {widget: this, url: url}));
            this.$el.find('> audio').remove()
            this.$el.prepend($audio);
            $audio.on('error', function() {
                instance.webclient.notification.warn(_t("Could not use sound file"));
            });
            
            
        }
    },
    on_file_uploaded_and_valid: function(size, name, content_type, file_base64) {
        this.binary_value = true;
        this.internal_set_value(file_base64);
        var show_value = name + " (" + instance.web.human_size(size) + ")";
        this.$el.find('input').eq(0).val(show_value);
        this.set_filename(name);
    },
    on_clear: function() {
        this._super.apply(this, arguments);
        this.$el.find('input').eq(0).val('');
        this.set_filename('');
    }
});


};

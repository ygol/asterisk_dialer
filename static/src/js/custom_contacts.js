openerp.asterisk_dialer = function(instance) {    
    instance.web.list.columns.add('field.html', 'instance.web.list.Html');
    
    instance.web.list.Html= instance.web.list.Column.extend({
        _format: function (row_data, options) {
            return row_data[this.id].value;
        }                                                                                                                                                                   
    });

};

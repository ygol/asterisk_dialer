{
    'name' : 'Asterisk Dialer',
    'description': '',
    'version' : '1.0',
    'depends' : ['base', 'mail', 'web',],
    'author' : 'litnimax',
    'website' : '',
    'category' : 'Asterisk',
    'data' : [
        'views/glyphicons.xml',
        'views/dialer_view.xml',
        'views/dialer_data.xml',
        'views/server_view.xml',
        'views/server_data.xml',
    ],
    'js': ['static/src/.*js'], 
    'auto_install': False,
    'installable': True,
}

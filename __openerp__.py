{
    'name' : 'Asterisk Dialer',
    'description': '',
    'version' : '1.0',
    'depends' : ['base', 'mail', 'web',],
    'author' : 'litnimax',
    'website' : '',
    'category' : 'Asterisk',
    'data' : [
        'views/dialer_view.xml',
        'views/dialer_data.xml',
        'views/server_view.xml',
        'views/server_data.xml',
    ],
    'auto_install': False,
    'installable': True,
}

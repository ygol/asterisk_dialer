#!/bin/bash

virtualenv env
source env/bin/activate

pip install http://download.gna.org/pychart/PyChart-1.39.tar.gz
pip install babel
pip install docutils
pip install feedparser
pip install gdata
pip install Jinja2
pip install mako
pip install mock
pip install psutil
pip install psycopg2
pip install pydot
pip install python-dateutil
pip install python-openid
pip install pytz
pip install pywebdav
pip install pyyaml
pip install reportlab
pip install simplejson
pip install unittest2
pip install vatnumber
pip install vobject
pip install werkzeug
pip install xlwt
pip install pyopenssl
pip install lxml
pip install python-ldap
pip install pillow
pip install decorator
pip install requests
pip install pyPdf
pip install wkhtmltopdf
pip install passlib
pip install ari

# Download Odoo
wget -c http://github.com/litnimax/odoo/archive/8.0.zip
unzip 8.0.zip && mv odoo-8.0 odoo


# Download asterisk dialer addon
mkdir myaddons
wget -c http://github.com/litnimax/odoo-asterisk-dialer/archive/master.zip
unzip master.zip && mv odoo-asterisk-dialer-master myaddons/asterisk_dialer


# Create a default Odoo config
cat > odoo/odoo.conf << EOF
[options]
addons_path = `pwd`/odoo/openerp/addons,`pwd`/odoo/addons,`pwd`/myaddons
admin_passwd = admin
data_dir = `pwd`/filestore
db_host = localhost
db_port = 5432
db_password = openerp
db_user = openerp
dbfilter = .*
debug_mode = False
log_level = warn
logfile = `pwd`/odoo-server.log
no-logrotate = True
without-demo=all
no-xmlrpc = True
no-xmlrpcs = True
no-netrpc = True

EOF

cd odoo

echo 'You can try to run Odoo from this dir ./odoo.py -c odoo.conf'

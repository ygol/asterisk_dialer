odoo-asterisk-dialer
====================

Asterisk dialer for Odoo. 

**Alpha release - expect bugs! **

## Installation
* Install [**my fork**](https://github.com/litnimax/odoo/) of Odoo 8.0. There is a bug in char_domain widget not yet fixed by the Odoo team so for now pls stick to mine. 
 * Checkout my Odoo 8.0:  ```git clone -b 8.0 https://github.com/litnimax/odoo.git odoo```
 * Or you can you [my install.sh](https://github.com/litnimax/odoo-asterisk-dialer/blob/master/install.sh) script to install Odoo & Asterisk dialer into virtual env in current directory.

* Install latest Asterisk. Versions < 12 do not have ARI support. 
* Install Asterisk ARI libs (pip install ari).
* Install odoo_asterisk_dialer module. Download from Github (using git clone or Download zip), rename folder to asterisk_dialer. Put this folder in your addons path (
see addons_path in Odoo configuration file (create initial one with ./odoo -s, CTRL+C and take it  from ~/.openerp_serverrc) or use an example configuration provided below.



## Documentation

### Odoo configuration
In Odoo you should set ''data_dir'' option.
We use it as a root folder for storing uploaded sound files.
Be sure this folder is accessible for UID which is asterisk running under.

#### Example configuration file
```
[options]
addons_path = /home/max/tmp-work/odoo/odoo/openerp/addons,/home/max/tmp-work/odoo/myaddons,/home/max/tmp-work/odoo/odoo/addons,
admin_passwd = admin
data_dir = /home/max/tmp-work/odoo/filestore
db_host = localhost
db_password = openerp
db_user = openerp
dbfilter = .*
debug_mode = False
log_level = info
logfile = False
```

### Asterisk settings

#### Dialplan

Dialer operates using ARI originate request. 
It connects each call to Asterisk dialplan with the following contents:

```
[dialer]
exten => _X.,1,Dial(SIP/${EXTEN}@peer_name,30,A(silence/2)); wait 2 sec for RTP to align.
exten => h,1,Set(res=${CURL(http://localhost:8069/dialer/channel_update/?channel_id=${UNIQUEID}&status=${DIALSTATUS}&answered_time=${ANSWEREDTIME})})
```

So you must add the above snippet to your extensions.conf. Replace *peer_name* with your provider's peer from sip.conf and *localhost:8069* with your Odoo instance URL.

Also set your own peer_name to provider's peer from  your sip.conf :-)

#### ARI settings
ARI is configured in ari.conf. Example of configuration:

```
allowed_origins = *
[username]
type = user
read_only = no
password = $6$GPX.W2HVNvy9Bo$EeHySUu89U8.Wg6BvJCWNv51bDhu82t8gNz1u5n83MH1qWK282G2zV4V4neFldBRNb.nVchmRq28EGFTYl4QH.
password_format = crypt
```
Password is generated with mkpasswd -m sha-512 or just use password_format = plain at your risk ;-) and put plain password here. 

Remember it will be transfered over the network in plain text and if Asterisk is in internet using plain is a security hole. 

Imagine a phone bill for $40,000 for calls to Inmarsat because Asterisk ARI access is sniffed.

### Running Dialer
Dialer operates in 2 modes (dialer type setting):

* Asterisk dialplan
* Playback message

#### Asterisk dialplan
When dialer type is set to *Playback* the Dialer originates calls and puts connected calls in specified Asterisk context name.

For example if instead of message playback we need to put every connected call in queue, the following dialplan must be created in extensions.conf:

```
[queue]
exten => _X.,1,Queue(test)
```
In Dialer configuration field *Context name* must be set to *queue*.

#### Playback message
In this mode Dialer plays uploaded sound file to called person.

## Managing Subscribers lists 
Dialer can dial either Contacts (Partners) or custom list of subscribers (phone numbers).
This lists can be imported from .csv files.

If .csv file has only one column with phone numbers, thay are *added to the last subcriber list created*.
If it has 2 columns (1st - for phonenumbers, 2nd - for subscriber list name) subscribers will be imported in the list specified.

## Troubleshooting
### Playback file is not played
Odoo saves sound files in a folder set by data_dir option. Check that Asterisk can read from there.
### Enable debug mode
Run Odoo with ''--log-level=debug'' and see errors.


## Feature requests 
The following features could be implemented if requested:

* Allow dialed person to press a key to confirm (aknowledge) the message
* Allow dialed person to be deleted from dialing list so that he will not be dialed on next round (like unsubscribe from list).
* Record dialed person choice in menu.
* Other.

To create a feature request [create](https://github.com/litnimax/odoo-asterisk-dialer/issues/new) a Github issue with label 'enhancement'.

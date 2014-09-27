odoo-asterisk-dialer
====================

Asterisk dialer for Odoo. 

**Work in progress. Pls don't use it yet.**


## Documentation

### Asterisk settings

#### Dialplan

Dialer operates using ARI originate request. 
It connects each call to Asterisk dialplan with the following contents:

```
[dialer]
exten => _X.,1,Dial(SIP/${EXTEN}@peer_name,30,A(silence/2)); wait 2 sec for RTP to align.
; Update unconnected call stats, connected calls are handled by Odoo Stasis app.
exten => _X.,n,Set(res=${CURL(http://localhost:8069/dialer/channel_update/?channel_id=${UNIQUEID}&status=${DIALSTATUS})})
```

So you must add the above snippet to your extensions.conf.

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
When dialer type is set to playback Dialer originate calls and puts connected calls in specified Asterisk context name.

For example if instead of message playback we need to put every connected call in queue, the following dialplan must be created in extensions.conf:

```
[queue]
exten => _X.,1,Queue(test)
```
In Dialer configuration field *Context name* must be set to *queue*.

#### Playback message
In this mode Dialer plays uploaded sound file to called person.


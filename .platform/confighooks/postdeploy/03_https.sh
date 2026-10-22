#!/bin/bash
# Environment property changes rebuild /etc/nginx too, so restore HTTPS afterwards.
exec /var/app/current/.platform/hooks/postdeploy/03_https.sh

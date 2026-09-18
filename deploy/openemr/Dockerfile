# OpenEMR with the Soft Clinical skin, so the EHR matches the Clinical Co-Pilot panel.
#
# Nothing OpenEMR ships is edited: the skin rides on custom/assets/custom.yaml, which \OpenEMR\Core\Header parses
# after config/config.yaml (src/Core/Header.php:184-188), so its <link> is emitted after the theme's on every page
# and wins the cascade. Remove the two files and the stock theme is back.
#
# The base is pinned by digest, not by the 8.5.0 tag: that tag was republished mid-week during this project and a
# redeploy silently ran a database upgrade (AUDIT.md OPS-4). This digest is the build that everything was tested on.
FROM openemr/openemr@sha256:65180ab275d97884bd917c88019c67c0e9e30e6591384b1d6572d2f5db312bc1

# Image files are apache-owned and read-only (400 files, 500 dirs); match that so the web user can read the skin.
COPY custom/assets/custom.yaml /var/www/localhost/htdocs/openemr/custom/assets/custom.yaml
COPY custom/assets/css/soft-clinical.css /var/www/localhost/htdocs/openemr/custom/assets/css/soft-clinical.css
RUN chown apache:apache /var/www/localhost/htdocs/openemr/custom/assets/custom.yaml \
        /var/www/localhost/htdocs/openemr/custom/assets/css/soft-clinical.css \
    && chmod 444 /var/www/localhost/htdocs/openemr/custom/assets/custom.yaml \
        /var/www/localhost/htdocs/openemr/custom/assets/css/soft-clinical.css

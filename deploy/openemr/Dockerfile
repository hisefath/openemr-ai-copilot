# OpenEMR with the Soft Clinical skin, so the EHR matches the Clinical Co-Pilot panel.
#
# Nothing OpenEMR ships is edited: the skin rides on custom/assets/custom.yaml, which \OpenEMR\Core\Header parses
# after config/config.yaml (src/Core/Header.php:184-188), so its <link> is emitted after the theme's on every page
# and wins the cascade. Remove the two files and the stock theme is back.
#
# The base is pinned by digest, not by the 8.5.0 tag: that tag was republished mid-week during this project and a
# redeploy silently ran a database upgrade (AUDIT.md OPS-4). This digest is the build that everything was tested on.
FROM openemr/openemr@sha256:65180ab275d97884bd917c88019c67c0e9e30e6591384b1d6572d2f5db312bc1

# Taller calendar rows. The 15-minute grid row height lives in PHP, not CSS: CalendarViewModel::computeEventGeometry
# multiplies $timeslotHeightVal into the top/height pixels it writes into each appointment's inline style attribute,
# and the theme's `.timeslot { height: 20px }` only sizes the gutter rows. Changing one without the other drifts every
# block by slotIndex x delta (36 slots a day here), so the constant moves in PHP and the skin matches it in CSS.
# sed -i writes the file back as root:root 0644; the image expects apache-owned 400 inside mode-500 directories.
RUN sed -i 's/\$timeslotHeightVal = 20;/\$timeslotHeightVal = 32;/g' \
        /var/www/localhost/htdocs/openemr/src/PostCalendar/ViewModel/CalendarRenderDataBuilder.php \
    && grep -c 'timeslotHeightVal = 32;' /var/www/localhost/htdocs/openemr/src/PostCalendar/ViewModel/CalendarRenderDataBuilder.php \
    && chown apache:apache /var/www/localhost/htdocs/openemr/src/PostCalendar/ViewModel/CalendarRenderDataBuilder.php \
    && chmod 400 /var/www/localhost/htdocs/openemr/src/PostCalendar/ViewModel/CalendarRenderDataBuilder.php

# Image files are apache-owned and read-only (400 files, 500 dirs); match that so the web user can read the skin.
COPY custom/assets/custom.yaml /var/www/localhost/htdocs/openemr/custom/assets/custom.yaml
COPY custom/assets/css/soft-clinical.css /var/www/localhost/htdocs/openemr/custom/assets/css/soft-clinical.css
RUN chown apache:apache /var/www/localhost/htdocs/openemr/custom/assets/custom.yaml \
        /var/www/localhost/htdocs/openemr/custom/assets/css/soft-clinical.css \
    && chmod 444 /var/www/localhost/htdocs/openemr/custom/assets/custom.yaml \
        /var/www/localhost/htdocs/openemr/custom/assets/css/soft-clinical.css

"""Email notifications via Gmail SMTP."""

import logging
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_email(subject, message, smtp_user, smtp_password, smtp_to):
    """Send an email via Gmail SMTP.

    Returns True on success, False on failure.
    """
    msg = MIMEText(message)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = smtp_to

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [smtp_to], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        log.error("Failed to send email: %s", e)
        return False

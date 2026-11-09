"""E-mail over SMTP (port 465 = SSL, 587 = STARTTLS), standard library only."""
import smtplib
import ssl
from email.message import EmailMessage


class MailError(Exception):
    """The e-mail could not be sent."""


class Mailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str = None):
        if not (host and user and password):
            raise MailError("e-mail is not set up: add SMTP_HOST, SMTP_USER and SMTP_PASSWORD to .env")
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender = sender or user

    def send(self, to: list, subject: str, body: str, reply_to: str = None) -> None:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, self.sender, ", ".join(to)
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(body)
        try:
            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port, context=ssl.create_default_context(), timeout=20)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=20)
                server.starttls(context=ssl.create_default_context())
            with server:
                server.login(self.user, self.password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"e-mail failed: {e}") from e

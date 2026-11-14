"""Phase 7 delivery: PDF rendering + (disabled-by-default) email sending.

PDF generation uses reportlab (pure-Python, no system Cairo/Pango
dependency like weasyprint needs -- picked because it installs cleanly in
this environment, per the master prompt's "pick what installs cleanly").

Email delivery is OFF by default (EMAIL_DELIVERY_ENABLED). Even when
enabled, it only ever fires for an `approved` briefing, only to a
recipient whose domain is in EMAIL_ALLOWED_RECIPIENT_DOMAINS, and is
idempotent per (briefingId, recipient) via the DeliveryLog unique
constraint -- see api.py's deliver_briefing route for the ordering of
those checks."""

import io
import logging
import os
from dataclasses import dataclass
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger("delivery")

DISCLAIMER_TEXT = (
    "This briefing is for informational purposes only and is not financial advice or a recommendation to "
    "buy, sell, or hold any security. Past performance is not indicative of future results. Consult a "
    "licensed financial advisor before making investment decisions."
)


def render_briefing_pdf(briefing: dict, client_name: str) -> bytes:
    """Renders one briefing to a PDF: title, content, sources list,
    compliance status, approval status/timestamp, and the disclaimer.
    Returns raw PDF bytes (never writes to disk -- the caller streams it
    straight back as an HTTP response body)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, topMargin=0.75 * inch, bottomMargin=0.75 * inch, pageCompression=0)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], spaceAfter=8)
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=8, textColor=colors.grey)

    story = [
        Paragraph(f"Morning Briefing — {client_name}", styles["Title"]),
        Paragraph(f"Generated: {briefing.get('createdAt', 'N/A')}", small),
        Spacer(1, 12),
    ]

    status = str(briefing.get("status", "draft"))
    status_rows = [["Compliance status", status]]
    if briefing.get("reviewedAt"):
        status_rows.append([f"{'Approved' if status == 'approved' else 'Reviewed'} at", str(briefing["reviewedAt"])])
    if briefing.get("reviewedBy"):
        status_rows.append(["Reviewed by", str(briefing["reviewedBy"])])
    status_table = Table(status_rows, colWidths=[2 * inch, 4 * inch])
    status_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ]
        )
    )
    story.append(status_table)
    story.append(Spacer(1, 16))

    # Content: paragraph-per-line is a reasonable approximation of the
    # writer's Markdown-ish output for a PDF (no Markdown renderer here --
    # the frontend's narrow custom renderer is for HTML, not PDF).
    for line in (briefing.get("content") or "").split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue
        if line.startswith("## "):
            story.append(Paragraph(line[3:], styles["Heading2"]))
        elif line.startswith("# "):
            story.append(Paragraph(line[2:], styles["Heading1"]))
        else:
            story.append(Paragraph(_escape(line), body))

    citations = briefing.get("citations") or []
    if citations:
        story.append(Spacer(1, 12))
        story.append(Paragraph("Sources", styles["Heading2"]))
        for c in citations:
            label = c.get("title") or c.get("url")
            story.append(Paragraph(f"&bull; {_escape(label)} — {_escape(c.get('url', ''))}", small))

    story.append(Spacer(1, 20))
    story.append(Paragraph(DISCLAIMER_TEXT, small))

    doc.build(story)
    return buf.getvalue()


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class DeliveryResult:
    sent: bool
    already_sent: bool = False
    error: Optional[str] = None


def _allowed_recipient_domains() -> set:
    return {d.strip().lower() for d in os.environ.get("EMAIL_ALLOWED_RECIPIENT_DOMAINS", "").split(",") if d.strip()}


def recipient_domain_allowed(recipient: str) -> bool:
    domain = recipient.rsplit("@", 1)[-1].lower() if "@" in recipient else ""
    return bool(domain) and domain in _allowed_recipient_domains()


def email_delivery_enabled() -> bool:
    return os.environ.get("EMAIL_DELIVERY_ENABLED", "false").lower() == "true"


def send_email(recipient: str, subject: str, pdf_bytes: bytes) -> DeliveryResult:
    """Sends the PDF via whichever provider EMAIL_PROVIDER names (resend or
    sendgrid). Callers (api.py) are responsible for checking
    email_delivery_enabled(), recipient_domain_allowed(), the briefing's
    approved status, and DeliveryLog idempotency BEFORE calling this --
    this function only does the actual send."""
    provider = os.environ.get("EMAIL_PROVIDER", "resend").lower()
    from_addr = os.environ.get("EMAIL_FROM_ADDRESS", "")
    if not from_addr:
        return DeliveryResult(sent=False, error="EMAIL_FROM_ADDRESS is not configured")

    import base64

    import requests

    attachment_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    try:
        if provider == "resend":
            api_key = os.environ.get("RESEND_API_KEY", "")
            if not api_key:
                return DeliveryResult(sent=False, error="RESEND_API_KEY is not configured")
            resp = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": from_addr,
                    "to": [recipient],
                    "subject": subject,
                    "text": "Your morning briefing is attached as a PDF.",
                    "attachments": [{"filename": "briefing.pdf", "content": attachment_b64}],
                },
                timeout=15,
            )
        elif provider == "sendgrid":
            api_key = os.environ.get("SENDGRID_API_KEY", "")
            if not api_key:
                return DeliveryResult(sent=False, error="SENDGRID_API_KEY is not configured")
            resp = requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "personalizations": [{"to": [{"email": recipient}]}],
                    "from": {"email": from_addr},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": "Your morning briefing is attached as a PDF."}],
                    "attachments": [{"filename": "briefing.pdf", "content": attachment_b64, "type": "application/pdf"}],
                },
                timeout=15,
            )
        else:
            return DeliveryResult(sent=False, error=f"Unknown EMAIL_PROVIDER '{provider}'")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Email send failed via {provider}: {e}")
        return DeliveryResult(sent=False, error=str(e))

    if resp.status_code >= 400:
        return DeliveryResult(sent=False, error=f"{provider} responded {resp.status_code}: {resp.text[:300]}")
    return DeliveryResult(sent=True)

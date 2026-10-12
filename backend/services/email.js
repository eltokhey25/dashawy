/**
 * @file   Outbound transactional email
 * @module services.email
 *
 * Thin wrapper around Resend (resend.com). Exports sendOtpEmail(), which delivers the
 * six-digit code used by the passwordless sign-in flow, plus isConfigured() so a route can
 * tell "no provider configured" from "the provider refused to send".
 *
 * @author Eltokhey
 * @since  2026-10
 */
// Transactional email through Resend.
//
// Kept behind a module like the other outbound integrations (google-identity,
// notifications) for one reason: nothing else in the app should ever hold the API
// key or build a message. A route asks for "send this code to this address" and
// has no way to leak the credential on the way.
//
// The key is read from the environment on every call rather than captured at
// load time. That is deliberate: this process is long-lived, and reading it late
// means a key added to the environment (a Railway variable set after a first
// boot) is picked up without a code change.
const logger = require('../utils/logger');

// The free tier only allows sending from this address, to the account owner's
// mailbox. Once a domain is verified in the Resend dashboard, set RESEND_FROM to
// something on it — the app does not care which address it is.
const DEFAULT_FROM = 'onboarding@resend.dev';

// The default verified by Resend for a fresh account. A second domain has to be
// verified in the dashboard before it can be used as a sender.
const DEFAULT_REPLY_TO = 'onboarding@resend.dev';

const OTP_SUBJECT = 'كود الدخول الخاص بك - دشاوي';

/**
 * Renders the one-time-code message.
 *
 * The `dir="rtl"` wrapper matters: the body is Arabic, and Resend delivers raw
 * HTML with no direction of its own, so without it the paragraph renders with
 * the digits and punctuation on the wrong side in every mail client.
 *
 * @param {string} otp  the six-digit code
 * @returns {string} HTML body
 */
function otpHtml(otp) {
  return [
    '<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;line-height:1.9">',
    '<p>كود الدخول الخاص بك هو: <strong>' + otp + '</strong></p>',
    '<p>الكود صالح لمدة 10 دقائق.</p>',
    '<p>إذا لم تطلب هذا الكود، تجاهل هذه الرسالة ولا تشاركها مع أحد.</p>',
    '</div>',
  ].join('');
}

/**
 * Whether an API key is present.
 *
 * @returns {boolean}
 */
function isConfigured() {
  return Boolean((process.env.RESEND_API_KEY || '').trim());
}

/**
 * @returns {string} the verified sender address for this deployment.
 */
function fromAddress() {
  return (process.env.RESEND_FROM || '').trim() || DEFAULT_FROM;
}

/**
 * Sends the sign-in code to an address.
 *
 * @param {string} toEmail  recipient address
 * @param {string} otp      the six-digit code
 * @returns {Promise<{sent: boolean, skipped?: boolean}>} `skipped` when no API key is
 *   configured — the caller is expected to carry on rather than fail.
 * @throws {Error} when a key IS configured but the provider refuses or fails to deliver.
 */
async function sendOtpEmail(toEmail, otp) {
  const apiKey = (process.env.RESEND_API_KEY || '').trim();

  // No key is the "feature not switched on for this deployment" case, which is
  // not a failure of the request: the caller logs the code and carries on. This
  // is what keeps a development checkout booting without a mail account, and
  // what keeps a production deploy that lost the variable from 500-ing every
  // sign-in.
  if (!apiKey) {
    logger.warn(
      'email',
      '[email] RESEND_API_KEY is not set — skipping the OTP email. Set it in the environment to enable email delivery.'
    );
    return { sent: false, skipped: true };
  }

  // Required lazily: a missing optional dependency must not stop the server from
  // booting, and by this point we already know the feature is in use.
  let Resend;
  try {
    ({ Resend } = require('resend'));
  } catch (err) {
    logger.error('email', '[email] the resend package is not installed:', err.message);
    return { sent: false, skipped: true };
  }

  const resend = new Resend(apiKey);
  const from = fromAddress();

  try {
    const { error } = await resend.emails.send({
      from,
      to: toEmail,
      subject: OTP_SUBJECT,
      html: otpHtml(otp),
      replyTo: DEFAULT_REPLY_TO,
    });
    // Resend reports a provider-side refusal in the resolved value rather than
    // by throwing, so the two cases are handled separately.
    if (error) {
      logger.error('email', '[email] Resend refused the message:', error.message || error);
      throw new Error(error.message || 'email provider error');
    }
    logger.info('email', '[email] OTP email sent to ' + toEmail + ' from ' + from);
    return { sent: true };
  } catch (err) {
    logger.error('email', '[email] could not send the OTP email to ' + toEmail + ':', err.message);
    throw err;
  }
}

module.exports = { sendOtpEmail, isConfigured };

import { LegalLayout, Section } from "./LegalLayout"

export default function Privacy() {
  return (
    <LegalLayout title="Privacy Policy" lastUpdated="May 1, 2026">
      <p>
        This describes what information ARCHIVE336 (the
        &ldquo;Service&rdquo;) collects, why, and what we do with it. If you
        have questions, email{" "}
        <a className="underline" href="mailto:support@archive336.com">
          support@archive336.com
        </a>
        . This policy applies to ARCHIVE336 only — other
        ARCHIVE336-branded products have their own privacy policies.
      </p>

      <Section title="What we collect">
        <p>
          <strong>Account information.</strong> Username, email address, and
          password (hashed with bcrypt — we never see your plaintext
          password). Date of account creation.
        </p>
        <p>
          <strong>YouTube connection (if you connect).</strong> Google OAuth
          access and refresh tokens, encrypted at rest using Fernet
          (AES-128-CBC + HMAC). Your Google account email (so we can show
          &ldquo;connected as X&rdquo;) and your YouTube channel ID and
          title.
        </p>
        <p>
          <strong>Archive data.</strong> For each video you archive: the
          video file itself, plus metadata such as title, description,
          tags, view count, upload date, and a record of when it was
          archived through the Service.
        </p>
        <p>
          <strong>Usage and billing.</strong> Daily snapshots of your total
          stored bytes (used to compute monthly invoices), records of
          invoices and their payment status, and your Stripe customer ID.
        </p>
        <p>
          <strong>Server logs.</strong> IP address, user agent, and
          timestamps of API requests. Kept for at most 30 days for security
          and abuse-prevention purposes.
        </p>
        <p>
          <strong>Payment information.</strong> Card details are entered
          directly into Stripe&rsquo;s hosted form. We never see or store
          them.
        </p>
      </Section>

      <Section title="How we use your information">
        <ul className="list-disc pl-6 space-y-1">
          <li>To operate the service — store archives, generate invoices, etc.</li>
          <li>
            To communicate with you about your account: receipts, security
            notices, important changes to terms or policies
          </li>
          <li>To investigate abuse or security incidents</li>
        </ul>
        <p>
          We do not sell your information. We do not use it for advertising.
        </p>
      </Section>

      <Section title="Third parties we use">
        <p>The following providers process some of your data:</p>
        <ul className="list-disc pl-6 space-y-1">
          <li>
            <strong>Stripe</strong> — payment processing. Card details and
            billing information are handled by Stripe under their privacy
            policy.
          </li>
          <li>
            <strong>Cloudflare R2</strong> — object storage for archived
            video files
          </li>
          <li>
            <strong>Cloudflare</strong> — CDN, DDoS protection, DNS for
            archive336.com
          </li>
          <li>
            <strong>Hetzner Online GmbH</strong> — server hosting (Ashburn,
            Virginia)
          </li>
          <li>
            <strong>Google</strong> — OAuth and YouTube Data API, when you
            connect a YouTube account
          </li>
        </ul>
        <p>
          Each provider has its own privacy policy that governs their
          handling of your data.
        </p>
      </Section>

      <Section title="Data retention">
        <p>
          We aim to keep your archived content as long as your account is
          active. After account closure, archived content may be preserved
          on our servers for up to 90 days to support reactivation, then
          deleted (subject to the exceptions below).
        </p>
        <p>
          You may request deletion of specific archives or your entire
          account at any time by emailing support@archive336.com. We will
          honor such requests within 30 days, except where retention is
          required for legitimate purposes — for example, tax records of
          past payments must be retained per IRS rules.
        </p>
      </Section>

      <Section title="Your rights (GDPR / CCPA)">
        <p>Depending on your jurisdiction, you may have the right to:</p>
        <ul className="list-disc pl-6 space-y-1">
          <li>Access the personal data we hold about you</li>
          <li>Correct inaccurate data</li>
          <li>Delete your data (&ldquo;right to erasure&rdquo;)</li>
          <li>
            Receive your data in a portable, machine-readable format
          </li>
          <li>
            Object to or restrict certain processing
          </li>
        </ul>
        <p>
          To exercise any of these rights, email support@archive336.com from
          the address on your account. We will respond within 30 days.
        </p>
      </Section>

      <Section title="Security">
        <ul className="list-disc pl-6 space-y-1">
          <li>Passwords are hashed with bcrypt</li>
          <li>
            OAuth tokens are encrypted at rest using Fernet (AES-128-CBC +
            HMAC). The encryption key lives in a file that&rsquo;s readable
            only by the application user, separately from the database
          </li>
          <li>All connections use HTTPS, with HSTS enforced at the edge</li>
          <li>
            Database backups are stored encrypted and access-controlled
          </li>
        </ul>
        <p>
          No system is perfectly secure. If you suspect a breach or have
          security concerns, email us immediately at support@archive336.com.
        </p>
      </Section>

      <Section title="Children's privacy">
        <p>
          ARCHIVE336 is not directed at children under 18 and is
          not intended for use by minors. If we become aware that we have collected
          personal information from a minor, we will delete the account.
        </p>
      </Section>

      <Section title="Cookies">
        <p>
          We use a single first-party session cookie (
          <code className="font-mono text-xs">archive336_session</code>) to keep
          you logged in. It contains an opaque random token that maps to a
          server-side session record. It&rsquo;s essential to the service
          and cannot be disabled while you&rsquo;re using the Service.
        </p>
        <p>
          We do not use tracking cookies or third-party advertising cookies.
        </p>
      </Section>

      <Section title="Changes to this policy">
        <p>
          We may update this policy. Material changes will be announced by
          email at least 14 days before they take effect.
        </p>
      </Section>

      <Section title="Contact">
        <p>
          Privacy questions:{" "}
          <a className="underline" href="mailto:support@archive336.com">
            support@archive336.com
          </a>
          .
        </p>
      </Section>
    </LegalLayout>
  )
}

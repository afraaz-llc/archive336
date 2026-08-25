import { LegalLayout, Section } from "./LegalLayout"

export default function Terms() {
  return (
    <LegalLayout title="Terms of Service" lastUpdated="May 22, 2026">
      <p>
        These Terms of Service (the &ldquo;Terms&rdquo;) govern your use of
        ARCHIVE336 (&ldquo;the Service&rdquo;, &ldquo;we&rdquo;,
        &ldquo;us&rdquo;), provided by Afraaz LLC. By creating an account or
        using ARCHIVE336, you agree to these Terms. If you
        don&rsquo;t agree, don&rsquo;t use the Service.
      </p>

      <Section number="1" title="The service">
        <p>
          ARCHIVE336 is an archival product that helps you preserve
          content from third-party platforms. At launch the Service is
          focused on YouTube channels you own or are authorized to
          archive.
        </p>
        <p>The Service works by:</p>
        <ul className="list-disc pl-6 space-y-1">
          <li>
            Authenticating you to YouTube via Google OAuth, with your explicit
            permission
          </li>
          <li>
            Coordinating downloads via yt-dlp running on your computer (the
            ARCHIVE336 desktop app) or, where supported, on our
            servers
          </li>
          <li>
            Storing the resulting files and metadata on cloud storage we operate
            (Cloudflare R2)
          </li>
        </ul>
        <p>
          We are a tool, not a content provider. Archived content is only
          accessible to the user who created the archive (and, in future
          shared-archive features, to other users that user has explicitly
          authorized).
        </p>
      </Section>

      <Section number="2" title="Eligibility">
        <p>
          You must be 18 or older to use ARCHIVE336. By creating an
          account, you represent that you are a legal adult in your
          jurisdiction and have the capacity to enter into these Terms.
        </p>
      </Section>

      <Section number="3" title="Account responsibilities">
        <ul className="list-disc pl-6 space-y-1">
          <li>You&rsquo;re responsible for keeping your password secure.</li>
          <li>
            You&rsquo;re responsible for any activity that occurs under your
            account.
          </li>
          <li>
            Notify us at support@archive336.com immediately if you believe your
            account has been compromised.
          </li>
        </ul>
      </Section>

      <Section number="4" title="Pricing and payment">
        <p>ARCHIVE336 uses pay-as-you-go pricing:</p>
        <ul className="list-disc pl-6 space-y-1">
          <li>
            <strong>$0.02 per GB stored, per month.</strong> Billed monthly on
            the 3rd. Storage usage rolls over month to month and is invoiced
            once it crosses $5.00.
          </li>
          <li>
            <strong>$1.00 annual membership fee,</strong> charged on the
            anniversary of the date you added your first payment method.
            Renewals are handled by Stripe Subscriptions and run
            automatically.
          </li>
        </ul>
        <p>
          All amounts are in US dollars. Payment is processed by Stripe, and
          we never see or store your card details. By providing payment
          information, you authorize us to charge it for amounts owed under
          these Terms.
        </p>
        <p>
          Charges are non-refundable except in two specific cases:
        </p>
        <ul className="list-disc pl-6 space-y-1">
          <li>
            <strong>Data we failed to retain.</strong> If our records
            show we billed you for storage of files we cannot actually
            produce on request (a discrepancy our reconciliation cron
            surfaces automatically), we will refund the charges
            associated with that data.
          </li>
          <li>
            <strong>Billing errors on our side.</strong> If a charge
            was calculated incorrectly, double-billed, or otherwise
            does not match the usage we actually delivered, we will
            correct it once you bring it to our attention.
          </li>
        </ul>
        <p>
          Storage and bandwidth charges otherwise represent
          infrastructure we have already paid for on your behalf and
          will not be refunded based on a change of mind. The annual
          membership fee is consumed at the moment of renewal and is
          not pro-rated upon cancellation; you may cancel auto-renewal
          at any time via the customer portal, and your access
          continues until the end of the paid year.
        </p>
        <p>
          To raise either type of refund request, email{" "}
          <a className="underline" href="mailto:support@archive336.com">
            support@archive336.com
          </a>
          .
        </p>
      </Section>

      <Section number="5" title="Acceptable use">
        <p>
          You agree to use the Service only to archive content you have the
          legal right to archive. This includes:
        </p>
        <ul className="list-disc pl-6 space-y-1">
          <li>Content you own (e.g. your own YouTube channel)</li>
          <li>Content in the public domain</li>
          <li>
            Content for which you have explicit permission from the rights
            holder
          </li>
          <li>
            Content for which your use qualifies as fair use under applicable
            copyright law
          </li>
        </ul>
        <p>You agree NOT to use the Service to:</p>
        <ul className="list-disc pl-6 space-y-1">
          <li>Archive content for commercial redistribution</li>
          <li>
            Circumvent technical protection measures, paywalls, or DRM
          </li>
          <li>Violate any third party&rsquo;s rights</li>
          <li>Engage in unlawful activity</li>
        </ul>
      </Section>

      <Section number="6" title="Third-party platforms">
        <p>
          When you connect a third-party platform (such as YouTube via
          Google OAuth), the Service interacts with that platform on your
          behalf using your authentication. You remain responsible for
          compliance with the third party&rsquo;s terms of service in your
          use of ARCHIVE336.
        </p>
        <p>
          We make no representation that the Service&rsquo;s use of any
          third-party API is endorsed by, sponsored by, or affiliated with
          that third party.
        </p>
      </Section>

      <Section number="7" title="Intellectual property">
        <p>
          You retain all rights to content you archive through the Service.
          By using ARCHIVE336, you grant us a limited,
          non-exclusive, royalty-free license to store, transmit, and
          process your archived content solely for the purpose of providing
          the Service to you.
        </p>
        <p>
          The &ldquo;ARCHIVE336&rdquo; name, the &ldquo;ARCHIVE336
          Tool&rdquo; product name and logo, the software, and the website
          are our property and may not be copied or reused without
          permission. Other ARCHIVE336-branded products (e.g. future ARCHIVE336
          Creator Network) are governed by their own separate terms.
        </p>
      </Section>

      <Section number="8" title="Copyright complaints (DMCA)">
        <p>
          If you believe content stored by the Service infringes your
          copyright, send a notice to support@archive336.com that includes:
        </p>
        <ul className="list-disc pl-6 space-y-1">
          <li>Identification of the copyrighted work claimed to be infringed</li>
          <li>
            Identification of the allegedly infringing material (a URL or
            other location)
          </li>
          <li>Your contact information</li>
          <li>
            A statement that you have a good-faith belief that the use is not
            authorized
          </li>
          <li>
            A statement, under penalty of perjury, that the information is
            accurate and that you are authorized to act on behalf of the
            rights holder
          </li>
          <li>Your physical or electronic signature</li>
        </ul>
        <p>
          Because archives are private to the user who created them,
          infringement claims will typically result in account-level review
          rather than public takedown.
        </p>
      </Section>

      <Section number="9" title="Termination">
        <p>
          You may close your account at any time via Settings. We may
          suspend or terminate your account if you materially breach these
          Terms, if we are required to do so by law, or if the service is
          being misused in a way that harms other users.
        </p>
        <p>
          Upon termination, your access ends immediately. Data retention
          following termination is described in our Privacy Policy.
        </p>
      </Section>

      <Section number="10" title="Disclaimers">
        <p>
          ARCHIVE336 is provided &ldquo;as is&rdquo; and &ldquo;as
          available&rdquo;. We make no warranty that the Service will be
          uninterrupted, error-free, or that data will never be lost.
          You should maintain your own backups of irreplaceable content.
        </p>
        <p>
          Third-party platforms may change their APIs, terms, or policies at
          any time, and may take action that affects the Service&rsquo;s
          ability to fetch or preserve content. We cannot guarantee
          continued compatibility.
        </p>
      </Section>

      <Section number="11" title="Limitation of liability">
        <p>
          To the maximum extent permitted by law, Afraaz LLC&rsquo;s total
          liability to you under these Terms is limited to the greater of
          $50 or the total amount you have paid us in the 12 months
          preceding the claim. We are not liable for indirect, incidental,
          consequential, or punitive damages.
        </p>
      </Section>

      <Section number="12" title="Governing law">
        <p>
          These Terms are governed by the laws of the State of Florida, USA,
          without regard to its conflict-of-laws rules. Any disputes shall
          be brought in the state or federal courts located in Florida.
        </p>
      </Section>

      <Section number="13" title="Changes to these Terms">
        <p>
          We may update these Terms over time. Material changes will be
          announced by email at least 14 days before they take effect.
          Continued use of ARCHIVE336 after the effective date
          constitutes acceptance.
        </p>
      </Section>

      <Section number="14" title="Contact">
        <p>
          Questions about these Terms? Email{" "}
          <a className="underline" href="mailto:support@archive336.com">
            support@archive336.com
          </a>
          .
        </p>
      </Section>
    </LegalLayout>
  )
}

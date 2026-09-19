# Add password reset via email

Implements the forgot-password flow from #412.

## Acceptance Criteria

- [ ] A user can request a password reset by submitting their email address to `POST /forgot-password`
- [ ] A reset token that expires after 1 hour is generated and emailed to the user
- [ ] Submitting a valid token and a new password to `POST /reset-password` updates the user's password and invalidates the token
- [ ] Requesting a reset for an email that does not exist returns the same success response as for a known email, so accounts cannot be enumerated
- [ ] Reset requests are rate limited to 5 per hour per IP address

## Notes

Email sending uses the existing `mailer` module.

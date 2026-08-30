# eToro App Store Readiness

Step 5 is an engineering foundation, not a submission-ready application.

Implemented foundations:

- explicit capabilities and operating modes
- runtime-only redacted credentials
- unique request identifiers
- bounded read retries and no blind write retry
- Demo-only exact route guard
- independent Risk Manager capability gate
- kill-switch activation after reconciliation mismatch
- secret-rejecting operational persistence
- offline deterministic tests

Still required before any distribution:

- official application registration and scope review
- privacy, security, retention, incident, and support policies
- live read-only sandbox validation with user consent
- broker eligibility/minimum-order preflight mapping
- end-to-end Demo reconciliation against official status endpoints
- threat model, penetration testing, observability, and recovery drills
- legal/compliance review and eToro approval

Real-money execution is absent and is not part of readiness.

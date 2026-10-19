# Scheduled read-only affiliate catalog collector

Python 3.12 collector running on standard GitHub-hosted Linux runners.
The schedule runs every six hours. API queries, local draft preparation and
historical counters only: no video generation or social publishing.

Only source code, authenticated ciphertext (`catalog-state.enc`) and a small
operational summary are public. Product selections, affiliate links, private
configuration and drafts remain inside AES-256-GCM encrypted state. Credentials
and encryption keys are separate GitHub Actions secrets, never files in Git.
State is decrypted only inside the ephemeral runner and authenticated before use.
Failures keep the previous encrypted state. Cache interval is at least six hours.

Do not add plaintext state, credentials, video files or user contact information.
No pull-request workflow or untrusted-code trigger has access to secrets.

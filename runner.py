"""Read-only affiliate collector. Plaintext state exists only in ephemeral storage."""
import json
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import cloud_collect
import encrypted_state
import production_gate


def main():
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary)
        encrypted_state.unseal(root/'catalog-state.enc', state)
        cloud_collect.ROOT = state
        with contextlib.redirect_stdout(io.StringIO()):
            cloud_collect.run(probe='--probe' in sys.argv)
        readiness = production_gate.inspect(state, cloud_collect.app.now())
        (root/'production-status.json').write_text(json.dumps(readiness,indent=2)+'\n')
        encrypted_state.seal(state, root/'catalog-state.enc')
        status = json.loads((state/'cloud-status.json').read_text())
        # Public status contains counters only, never listings, links or scripts.
        public = {key:status.get(key) for key in ('status','checked_at','collected_at',
                  'offers','scripts','pending_models','publishing_enabled','run_url')}
        public['api_authentication_test'] = '--probe' in sys.argv
        (root/'runner-status.json').write_text(json.dumps(public,indent=2)+'\n')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Collection failed; previous encrypted state preserved. No sensitive details logged.', file=sys.stderr)
        sys.exit(1)

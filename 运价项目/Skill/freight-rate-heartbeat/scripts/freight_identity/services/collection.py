"""Pure collection policy. Only confirmed publisher identities enter parsing."""
from freight_identity.services.publisher import identity_key
from freight_identity.core.contracts import PUBLISHER, EXCLUDED, SHADOW_MODE


def collection_decision(message, assessment, revision):
    ready = bool(assessment.get('identity_ready'))
    status = assessment.get('status', 'insufficient')
    excluded = ready and status == EXCLUDED
    recognized = status == PUBLISHER
    # Unknown/candidate identities remain in raw evidence and the audit queue,
    # but cannot create quote rows in publisher-only collection mode.
    suppressed = not recognized
    return {'ok': True, 'data': {
        'identity_key': identity_key(message), 'roster_revision': revision,
        'mode': SHADOW_MODE, 'status': status,
        'basis': assessment.get('basis', 'insufficient'),
        'action': 'parse' if recognized else 'exclude',
        'shadow_action': 'parse' if recognized else 'identity_review',
        'identity_ready': ready, 'reasons': assessment.get('reasons', []),
        'suppressed': suppressed,
    }}

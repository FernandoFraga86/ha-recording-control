# Home Assistant Recording Control — source-only experimental snapshot

**Status: EXPERIMENTAL SOURCE ONLY — NOT INSTALLABLE — NO CONTAINER IMAGE PUBLISHED.**

This repository contains a source patch for an unofficial Home Assistant app that integrates with Frigate, plus Supervisor metadata templates. It is for review only. It is not an official Frigate or Home Assistant project and is not ready for installation or production use.

## Frozen source identity

- Upstream base: Frigate 0.18.2, commit `cfa08c56b23d23280c3d0a218e5a43182c97445b`.
- Candidate source snapshot: `1ca90f845603cd30e1dd241fd9e3ce6506a36841`.
- Source overlay SHA-256: `acc70d496dc667a2b44241b4bf527d6d8e8fe08a2e47d32ff4667598d45ff6be`.
- Published patch file SHA-256: `512f404a274aeb11a3e26246cddae48a62539cc01ccb10d6a297c5ea42221b65`.
- Candidate app/image version string: `0.18.2-recording-control-1ca90f8`.
- Planned image reference: `ghcr.io/fernandofraga86/ha-recording-control:0.18.2-recording-control-1ca90f8`.

The repository contains only the source diff against the upstream base, its provenance, and `.template` Supervisor files. It does not include the full upstream source tree, the locally built image, Frigate logo/brand assets, household configuration, camera addresses or credentials, recordings, backups, or host/VM identifiers.

For privacy, one synthetic username in a test fixture was generalized from a household-identifiable name to `test_viewer`. No runtime source hunk was changed by this publication-only sanitization; the overlay digest above identifies the original frozen build snapshot, while the patch hash identifies this sanitized public patch.

The app template intentionally retains the candidate's Supervisor API, Home Assistant API, MQTT, media, app-configuration, and Home Assistant configuration mappings. Their final necessity and privacy impact have not been revalidated for this public candidate; do not install the template.

## Distribution and validation status

The planned image reference is not published and has no verified OCI digest. The target metadata records Supervisor 2026.10.5 and `amd64` as intended review targets only; loading, installation, recovery, privacy, and startup behavior have not been validated against that Supervisor in this publication. Historical test summaries are not represented as a run bound to this exact public commit.

The app templates deliberately retain the `.template` suffix. This repository has no root `repository.yaml`, and no app file is named `config.yaml`; Home Assistant therefore cannot discover this source-only snapshot as an installable app repository. Do not rename the templates or install this candidate.

The source-derived version string is content-identifying, but registry tag immutability has not been configured or verified. A future runnable release must publish the image, verify and record its OCI digest, validate the complete app with the target Supervisor, and keep the app non-installable until those gates pass.

## Trademark and attribution

The upstream source is MIT-licensed; retain its copyright and license notice. Frigate names and visual brand assets are not licensed by MIT. “Frigate” is used here only to identify compatibility. No Frigate logo is included. A future distributed runnable derivative requires a separate branding review and must not imply endorsement by Frigate, Inc. or the Home Assistant project.

See `LICENSE`, `UPSTREAM-TRADEMARK-POLICY.md`, `SOURCE-PROVENANCE.json`, and `distribution-lock.json`.

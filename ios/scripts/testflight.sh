#!/usr/bin/env bash
# Archive the app and upload it to TestFlight — pure CLI, no Xcode UI.
#
# Prereqs (one-time):
#   - App Store Connect app record for com.travismetcalf.commute (created in
#     the ASC web UI; the CLI cannot create records)
#   - Xcode signed into the Apple ID that owns team 8F62W24942
#
# Build number = git commit count: monotonically increasing, reproducible,
# and never needs manual bumping. MARKETING_VERSION stays in project.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD_NUM=$(git rev-list --count HEAD)
ARCHIVE=build/Commute.xcarchive

echo "==> xcodegen (build $BUILD_NUM)"
xcodegen >/dev/null

echo "==> archiving"
xcodebuild archive \
  -scheme Commute \
  -destination 'generic/platform=iOS' \
  -archivePath "$ARCHIVE" \
  -allowProvisioningUpdates \
  CURRENT_PROJECT_VERSION="$BUILD_NUM" \
  -quiet

# Export options: app-store-connect method re-signs with the distribution
# profile (aps-environment flips to production automatically); destination
# "upload" sends the build straight to ASC instead of writing an .ipa.
cat > build/ExportOptions.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key><string>app-store-connect</string>
    <key>destination</key><string>upload</string>
    <key>teamID</key><string>8F62W24942</string>
    <key>uploadSymbols</key><true/>
    <key>manageAppVersionAndBuildNumber</key><false/>
</dict>
</plist>
PLIST

echo "==> uploading to App Store Connect"
xcodebuild -exportArchive \
  -archivePath "$ARCHIVE" \
  -exportOptionsPlist build/ExportOptions.plist \
  -allowProvisioningUpdates \
  -quiet

echo "==> uploaded build $BUILD_NUM — it appears in TestFlight after"
echo "    processing (~5-15 min); internal testers need no review."

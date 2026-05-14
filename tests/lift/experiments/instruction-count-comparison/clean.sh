#!/usr/bin/env bash
# Remove all generated output directories.
cd "$(dirname "$0")"
rm -rf out_*/
echo "cleaned out_*/"

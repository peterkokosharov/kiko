#!/bin/bash
# Git auto-commit and push script for kiko-tqt3
# LLM: gemini-flash-lite

echo "Adding kiko-tqt3 and test plan..."
cd /home/petark/kiko/kiko
git add kiko-tqt3/ docs/TEST_PLAN.md

echo "Committing changes..."
git commit -m "Add kiko-tqt3 TDE panel applet, test app, and test plan

Co-Authored-By: Claude Code <noreply@anthropic.com>"

echo "Pushing to remote origin (https://github.com/peterkokosharov/kiko.git)..."
git push origin main

echo "Done!"

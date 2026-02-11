# Railway Environment Setup

## Required Environment Variables

Make sure these are set in your Railway project settings:

1. **BOT_TOKEN** - Your Telegram bot token
   ```
   8050562947:AAEvPkSUksllHuxb5b7KLtLLPHsDAGH0xqI
   ```

2. **ADMIN_IDS** - Comma-separated admin user IDs
   ```
   6006281662,5694267817
   ```

3. **SUPPORT_GROUP_ID** - Your support group chat ID
   ```
   -1003786439934
   ```

4. **WEBAPP_URL** - Your Railway app URL with webapp.html
   ```
   https://worker-production-ed30.up.railway.app/webapp.html
   ```

5. **PORT** - Railway should set this automatically, but if not:
   ```
   8080
   ```

## How to Set Variables in Railway

1. Go to your Railway dashboard
2. Click on your project
3. Click on the "Variables" tab
4. Add each variable above (without spaces in ADMIN_IDS)
5. Railway will automatically redeploy

## Verify Deployment

After Railway redeploys:
1. Check the deployment logs for any errors
2. Test the bot with `/start` command
3. Test the mini app with `/menu` command
4. Verify menu updates in real-time

## What Was Fixed

- **Caching Issue**: Added aggressive cache-busting with random values
- **Headers**: Added proper no-cache headers to prevent caching at all levels
- **Polling**: Reduced polling interval from 10s to 5s for faster updates
- **Visibility**: Added listener to refresh when app becomes active
- **WEBAPP_URL**: Configured the mini app URL for menu functionality

The bot should now properly sync menu items in real-time with chadsflooring.bz!
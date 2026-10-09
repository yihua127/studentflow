# Connect Gmail to StudentFlow

StudentFlow runs on your computer. It uses Google sign-in with read-only access to display your inbox and open selected messages.

## First-time setup

1. Sign in to the [Google Cloud console](https://console.cloud.google.com/) with your personal Google account. Create and select a project named **StudentFlow**.
2. Under **APIs & Services → Library**, find and enable **Gmail API**.
3. Open **Google Auth Platform → Branding**. Enter **StudentFlow** as the app name, and your email for support and contact details.
4. Set **Audience** to **External**, keep the app in **Testing**, and add your Gmail address under **Test users**. Add other users who need to connect their own Gmail accounts.
5. Under **Data Access**, add only this scope: `https://www.googleapis.com/auth/gmail.readonly`.
6. Under **Clients**, create a **Desktop app** client and download its JSON configuration. Save it as `credentials/gmail_client.json` inside the StudentFlow project. Keep the file out of GitHub.

Creating a Google Cloud project registers StudentFlow for Google sign-in. See Google's [OAuth setup guide](https://developers.google.com/workspace/guides/configure-oauth-consent) and [Gmail desktop quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).

## Sign in and open a message

1. Start StudentFlow and open **Inbox (Gmail)**.
2. Expand **Gmail connection**, check **Allow StudentFlow to read my Gmail messages**, and select **Connect Gmail**.
3. Complete Google sign-in in the browser. Choose an account listed under Test users and review the read-only permission request.
4. Return to StudentFlow and select **Refresh inbox**.
5. Choose a message and select **Open message**. Its text and attachments are loaded for review.
6. Add a task using a passage from the message as supporting text. Review it before selecting **Save task**.

If Google shows a verification notice, verify that the application is your own StudentFlow project and the permission is read-only. If access is blocked, check the project and test-user settings instead of using another application's credentials.

## Personal data and sign-out

- AI task extraction uses a separately configured model and explicit consent for each selected message.
- Sign-in tokens are saved in `data/auth/` on your computer. Do not share that folder. Each person should run a separate copy and sign in with their own account.
- **Disconnect Gmail** removes the saved sign-in on this device and clears opened messages. Previously saved tasks remain. To revoke Google's permission for StudentFlow, remove it from your Google account's third-party connections.
- The message count limits each inbox query; the OAuth permission itself allows read access to the account's mail.
- In External + Testing mode, refresh tokens for this scope expire after seven days. Sign in again when prompted. [Google token documentation](https://developers.google.com/identity/protocols/oauth2#expiration)

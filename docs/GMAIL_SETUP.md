# Connect Gmail to StudentFlow

StudentFlow runs on your computer. It uses Google sign-in with read-only access to display your inbox and open selected messages.

## First-time setup

1. Sign in to the [Google Cloud console](https://console.cloud.google.com/) with your personal Google account. Create and select a project named **StudentFlow**.
2. Under **APIs & Services → Library**, find and enable **Gmail API**.
3. Open **Google Auth Platform → Branding**. Enter **StudentFlow** as the app name, and your email for support and contact details.
4. Set **Audience** to **External**, keep the app in **Testing**, and add your Gmail address under **Test users**. Add other users who need to connect their own Gmail accounts.
5. Under **Data Access**, add `https://www.googleapis.com/auth/gmail.readonly` for the inbox. Optional reply sending uses the additional scopes described below.
6. Under **Clients**, create a **Desktop app** client and download its JSON configuration. Save it as `credentials/gmail_client.json` inside the StudentFlow project. Keep the file out of GitHub.

Creating a Google Cloud project registers StudentFlow for Google sign-in. See Google's [OAuth setup guide](https://developers.google.com/workspace/guides/configure-oauth-consent) and [Gmail desktop quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).

## Sign in and open a message

1. Start StudentFlow and open **Inbox (Gmail)**.
2. Expand **Gmail connection**, check **Allow StudentFlow to read my Gmail messages**, and select **Connect Gmail**.
3. Complete Google sign-in in the browser. Choose an account listed under Test users and review the read-only permission request.
4. Return to StudentFlow and select **Refresh inbox**. Select **Load more** to continue browsing additional messages.
5. In **Message analysis**, select **Analyze selected messages** after giving AI consent to extract flexible tasks and fixed events. To draft a reply, select one message and choose **Reply with assistant**; no separate View step or second selection is required.
6. Add a task using a passage from the message as supporting text. Review it before selecting **Save task**.

Your sign-in is saved on this device. Reopening StudentFlow restores the connection and displays **Gmail connected**. Use **Switch account** to select another Gmail account, or **Disconnect Gmail** to sign out. A temporary connection issue can be retried with **Retry connection**; **Reconnect Gmail** completes a new authorization when requested by Google.

If Google shows a verification notice, verify that the application is your own StudentFlow project and the requested permissions match the feature you are connecting. If access is blocked, check the project and test-user settings instead of using another application's credentials.

## Enable reviewed reply sending

Under the same project's **Google Auth Platform → Data Access**, add `https://www.googleapis.com/auth/gmail.send`, `openid` and `https://www.googleapis.com/auth/userinfo.email`. In a reply draft, expand **Send with Gmail → Connect Gmail sending** and select the same account as Inbox. The app stores this authorization separately in `data/auth/gmail_send_token.json`; the existing Inbox token retains its read-only scope.

The send-only Gmail scope cannot call the Gmail profile endpoint, so OpenID email identity verifies the sender account. See Google's [Gmail scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes), [profile API permissions](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile), and [OpenID Connect guide](https://developers.google.com/identity/openid-connect/openid-connect). Replies include the original thread and message references and keep the original subject, following Google's [threading requirements](https://developers.google.com/workspace/gmail/api/guides/threads).

After authorization, review the exact recipient and message in StudentFlow, then explicitly press **Send with Gmail**. The model cannot initiate this action. An uncertain send remains blocked locally; check Gmail Sent before attempting further action. To revoke sending permission, remove the app's access in Google account settings. **Disconnect Gmail** only removes the Inbox connection and disables this flow until that inbox is connected again; sending uses its separate saved authorization.

## Personal data and sign-out

- AI task extraction uses a separately configured model and explicit consent for each selected message.
- Sign-in tokens are saved in `data/auth/` on your computer. Do not share that folder. Each person should run a separate copy and sign in with their own account.
- **Disconnect Gmail** removes the saved sign-in on this device and clears opened messages. Previously saved tasks remain. To revoke Google's permission for StudentFlow, remove it from your Google account's third-party connections.
- The inbox loads up to 20 emails at a time. **Load more** adds the next 20 to the inbox list.
- In External + Testing mode, refresh tokens for this scope expire after seven days. Sign in again when prompted. [Google token documentation](https://developers.google.com/identity/protocols/oauth2#expiration)

# OpenAI setup

StudentFlow uses an OpenAI API key to extract task suggestions from selected email messages.

## 1. Add API credit

1. Sign in to the [OpenAI API platform](https://platform.openai.com/).
2. Open [Billing](https://platform.openai.com/settings/organization/billing/overview), add a payment method, and purchase API credit using **Buy credits**, **Add to balance**, or the payment option shown for your account. A small initial balance is enough to begin testing; confirm the amount shown before paying.
3. Review your spending in the [Usage dashboard](https://platform.openai.com/usage).

API usage has its own billing, separate from ChatGPT Plus or Pro. See the [official pricing overview](https://learn.chatgpt.com/docs/pricing) and [API quickstart](https://developers.openai.com/api/docs/quickstart).

## 2. Create a key

1. Select or create a project named **StudentFlow** in the API platform.
2. Open [API keys](https://platform.openai.com/api-keys), choose **Create new secret key**, and name it **StudentFlow Local**.
3. Copy the key and paste it directly into StudentFlow's **Settings** tab.

The [official API quickstart](https://developers.openai.com/api/docs/quickstart) explains key creation and storage.

## 3. Save it in StudentFlow

1. Open **Settings**.
2. Paste the key into **OpenAI API key**.
3. Keep **Model ID** as `gpt-5.4-mini` and select **Save AI settings**.
4. Select **Test AI connection**. This sends a short built-in example and incurs API usage charges.
5. A successful check displays **AI connection successful. Task extraction is ready.**

The key is stored in the local `.env` file, which Git ignores. To change the model later, enter the new model ID and save; leaving the key field blank retains the saved key.

[GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) supports the structured responses used by StudentFlow. Check the model page for current token prices and availability.

## 4. Extract tasks

Open **Inbox (Gmail)**, load your messages, and use the AI extraction controls. Confirm the messages to share with OpenAI, run extraction, then review the suggested tasks before saving them.

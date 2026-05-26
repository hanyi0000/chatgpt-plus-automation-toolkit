QQ 邮箱来源使用说明

1. 在 QQ 邮箱设置中启用 IMAP/SMTP 服务并生成授权码。
2. 在 accounts.txt 和 mail_pool.txt 中填写同一批可用账号：

   user@qq.com----IMAP授权码

3. 将 .env 中 MAIL_SOURCE 或 FLOW1_MAIL_SOURCE 设置为 qq。

支持 @qq.com、@foxmail.com 与 @vip.qq.com 邮箱。授权码属于敏感信息，请勿提交真实内容到仓库。

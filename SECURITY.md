# Security policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub’s private vulnerability reporting for this repository: **Security → Report a vulnerability**.

Include the AutoResume version, operating system, provider CLI/version, reproduction steps, and impact. Remove tokens, transcript contents, account identifiers, and other secrets from reports and logs. You can expect an acknowledgement within seven days.

## Scope

Especially useful reports involve unintended terminal input, composer-draft corruption, unsafe settings restoration, command injection through status-line chaining, disclosure of transcript/authentication data, or retrying a failure category that the security model excludes.

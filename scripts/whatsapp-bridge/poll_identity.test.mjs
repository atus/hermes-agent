import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { matchesAllowedUser } from './allowlist.js';
import * as helpers from './bridge_helpers.js';

test('poll vote attribution preserves the human allowlist identity', () => {
  const botId = '100000000000001@lid';
  const voterPhone = '15550001111';
  const voterJid = `${voterPhone}@s.whatsapp.net`;
  const voterLid = '100000000000002@lid';
  const groupId = '120363000000000001@g.us';
  const sessionDir = mkdtempSync(path.join(tmpdir(), 'wa-poll-identity-'));
  const allowedUsers = new Set([voterPhone]);

  try {
    writeFileSync(
      path.join(sessionDir, 'lid-mapping-100000000000002_reverse.json'),
      JSON.stringify(voterPhone),
    );
    assert.equal(typeof helpers.resolvePollVoterId, 'function');
    for (const identity of [voterJid, voterLid]) {
      for (const voteFields of [
        { voterId: identity },
        { pollUpdateKey: { participant: identity } },
      ]) {
        const sender = helpers.resolvePollVoterId({
          ...voteFields,
          pollCreationKey: { participant: botId },
          chatId: groupId,
        });
        assert.equal(sender, identity);
        assert.equal(matchesAllowedUser(sender, allowedUsers, sessionDir), true);
      }
    }
    assert.equal(matchesAllowedUser(botId, allowedUsers, sessionDir), false);
  } finally {
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

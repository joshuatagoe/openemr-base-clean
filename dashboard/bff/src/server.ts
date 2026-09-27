// Entry point: load config from the environment and listen.
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildApp } from './app.js';
import { loadConfig } from './config.js';

const here = dirname(fileURLToPath(import.meta.url));
const env = { ...process.env };
// Default: the web build next to this package (dashboard/web/dist).
env.WEB_DIST_DIR ??= resolve(here, '../../web/dist');

const config = loadConfig(env);
const app = await buildApp({ config });

const shutdown = async () => {
  await app.close();
  process.exit(0);
};
process.on('SIGINT', () => void shutdown());
process.on('SIGTERM', () => void shutdown());

await app.listen({ host: config.host, port: config.port });

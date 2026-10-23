import { config } from './config.js';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

const PDF_MAGIC = '%PDF-';
const MAX_REDIRECTS = 5;

function ipv4ToNumber(address) {
  return address.split('.').reduce((value, part) => ((value << 8) | Number(part)) >>> 0, 0);
}

function isPublicAddress(address) {
  if (isIP(address) === 4) {
    const value = ipv4ToNumber(address);
    const blocked = [
      ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8],
      ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24], ['192.0.2.0', 24],
      ['192.88.99.0', 24], ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24],
      ['203.0.113.0', 24], ['224.0.0.0', 4], ['240.0.0.0', 4],
    ];
    return !blocked.some(([network, bits]) => {
      const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
      return (value & mask) === (ipv4ToNumber(network) & mask);
    });
  }

  if (isIP(address) !== 6) return false;
  const normalized = address.toLowerCase().split('%')[0];
  if (normalized.startsWith('::ffff:')) {
    const mapped = normalized.slice(7);
    return isIP(mapped) === 4 && isPublicAddress(mapped);
  }
  // Global unicast is 2000::/3. Exclude documentation, benchmarking and ORCHID ranges.
  if (!/^2[0-9a-f]{3}:/.test(normalized)) return false;
  return !/^2001:(?:0db8|000[01]|0010):/.test(normalized);
}

async function validatePublicUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('URL 格式无效');
  }
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('仅支持 HTTP 或 HTTPS 链接');
  if (url.username || url.password) throw new Error('链接不能包含用户名或密码');
  if (url.port && !['80', '443'].includes(url.port)) throw new Error('仅允许访问 HTTP/HTTPS 标准端口');

  const host = url.hostname.replace(/^\[|\]$/g, '').replace(/\.$/, '').toLowerCase();
  if (host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local')) {
    throw new Error('不允许访问本机或本地域名');
  }
  const addresses = isIP(host) ? [host] : (await lookup(host, { all: true })).map(({ address }) => address);
  if (!addresses.length || addresses.some((address) => !isPublicAddress(address))) {
    throw new Error('不允许访问非公网地址');
  }
  return url;
}

async function safeHttpGet(value, options) {
  let url = value;
  for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects++) {
    const parsed = await validatePublicUrl(url);
    const res = await fetch(parsed, {
      ...options,
      redirect: 'manual',
      signal: AbortSignal.timeout(config.fetchTimeoutMs),
      headers: {
        'User-Agent': config.userAgent,
        Accept: 'text/html,application/xhtml+xml,application/pdf,application/x-pdf,*/*;q=0.8',
        ...options?.headers,
      },
    });
    if (res.status < 300 || res.status >= 400) return res;
    const location = res.headers.get('location');
    if (!location) return res;
    await res.body?.cancel();
    if (redirects === MAX_REDIRECTS) throw new Error('网页重定向次数过多');
    url = new URL(location, parsed).href;
  }
  throw new Error('网页重定向次数过多');
}

function looksLikePdfUrl(url) {
  try {
    const u = new URL(url);
    const p = u.pathname.toLowerCase();
    return p.endsWith('.pdf') || p.includes('.pdf?');
  } catch {
    return false;
  }
}

async function httpGet(url, { binary = false } = {}) {
  const res = await safeHttpGet(url);
  if (!res.ok) {
    throw new Error(`抓取失败 HTTP ${res.status} ${res.statusText}`);
  }
  const contentType = (res.headers.get('content-type') || '').toLowerCase();
  const body = binary ? Buffer.from(await res.arrayBuffer()) : await res.text();
  return { body, contentType, finalUrl: res.url || url };
}

/**
 * 下载并识别来源类型：PDF 论文 或 网页。
 * @returns {{type:'pdf', buffer:Buffer, finalUrl:string} | {type:'webpage', html:string, finalUrl:string}}
 */
export async function fetchSource(url) {
  const raw = await httpGet(url, { binary: true });

  const isPdf =
    looksLikePdfUrl(url) ||
    raw.contentType.includes('pdf') ||
    raw.body.subarray(0, 5).toString() === PDF_MAGIC;

  if (isPdf) {
    return { type: 'pdf', buffer: raw.body, finalUrl: raw.finalUrl };
  }

  // 网页：重新以文本方式取回（避免 buffer 转码问题），兼容 gbk 等常见编码兜底
  let html;
  try {
    const text = await httpGet(url);
    html = text.body;
  } catch {
    html = raw.body.toString('utf-8');
  }
  return { type: 'webpage', html, finalUrl: raw.finalUrl };
}

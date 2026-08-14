// 简单 HTTP 代理：把容器内 localhost:<port> 代理到 docker service:<port>
// 用于在容器内跑 playwright 时的 API 调用。
// 用法：node _proxy.mjs <listenPort> <targetHost> <targetPort>
import net from 'node:net';
const [, , listenPort, targetHost, targetPort] = process.argv;
const lp = Number(listenPort);
const tp = Number(targetPort);
const server = net.createServer((client) => {
  const upstream = net.connect(tp, targetHost, () => {
    client.pipe(upstream).pipe(client);
  });
  upstream.on('error', () => client.destroy());
  client.on('error', () => upstream.destroy());
});
server.listen(lp, '0.0.0.0', () => console.log(`proxy listening on :${lp} → ${targetHost}:${tp}`));
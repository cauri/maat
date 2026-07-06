// Container healthcheck (Dockerfile HEALTHCHECK → /healthz).
export function GET() {
  return new Response("ok");
}

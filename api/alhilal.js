export default async function handler(req, res) {
  const apiKey = process.env.API_FOOTBALL_KEY;

  if (!apiKey) {
    return res.status(500).json({
      error: "مفتاح API غير موجود"
    });
  }

  const response = await fetch(
    "https://v3.football.api-sports.io/fixtures?team=2969&next=5",
    {
      headers: {
        "x-apisports-key": apiKey
      }
    }
  );

  const data = await response.json();

  return res.status(200).json(data);
}

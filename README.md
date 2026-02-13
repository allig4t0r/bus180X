# bus180X
GTFS sometimes works

St. Petersburg GTFS

```https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs```

Start:

```docker compose up -d```

Check logs:

```docker compose logs -f```

Stop:

```docker compose down -t 15```


For cron:

```
00 7 * * MON-FRI cd /root/bus180 && docker compose up -d
59 7 * * MON-FRI cd /root/bus180 && docker compose logs >> bus180.log
00 8 * * MON-FRI cd /root/bus180 && docker compose down -t 15
```
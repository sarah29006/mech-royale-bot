# Fieldline

Fieldline is a placement-first Python bot for Builders’ Mech Royale. It forecasts the shrinking zone, routes around arena geometry, collects nearby loot, and uses weapon-specific aiming. It reads the mounted arena at startup. The image uses only the Python standard library and can run as UID 65534 with a read-only filesystem.

`mechfight.py` is the transport wrapper from the official Mech Royale starter kit. The bot and arena logic are in `bot.py` and `arena.py`.

## Windows protocol check

From this repository, with the extracted organizer kit in `../mechfight-kit`, run this in PowerShell.

```powershell
python -B ..\mechfight-kit\check\check.py -- python -B -u bot.py
```

## Linux container check

From the repository in a Linux shell with Docker available, run the organizer’s check under the match limits.

```sh
docker build --platform linux/amd64 -t fieldline .
python3 ../mechfight-kit/check/check.py --map-dir /arena -- \
  docker run --rm -i --platform linux/amd64 --network none --cpus 1 --memory 512m \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "$(cd ../mechfight-kit/arena && pwd):/arena:ro" fieldline
```

The recorded stream checks the message format and timing. A live practice submission is needed to measure placement.

## Submit

Push the repository to an empty, public GitHub repository with `Dockerfile` at the top level. On the [Mech Royale track page](https://www.joinbuilders.org/buildathon/tracks/builders), submit the repository URL and the commit hash shown by `git rev-parse HEAD`. Keep the repository and that commit public through the final results.

"""A placement-first entry for the Builders Mech Royale simulator."""

import math
from time import perf_counter

from arena import Arena
import mechfight as mf


WEAPON_VALUE = {"gun": 1, "mortar": 2, "lightning": 3, "laser": 4}
UPGRADE_VALUE = {
    "armor": 9, "plating": 10, "nanites": 9, "servos": 8, "reactor": 7,
    "jets": 6, "sensors": 5, "capacitor": 5, "gyros": 4,
    "laser_damage": 9, "laser_charge": 8, "laser_pierce": 2,
    "lightning_range": 8, "lightning_efficiency": 7, "lightning_chain": 3,
    "gun_mag": 7, "gun_damage": 6, "gun_rate": 5, "gun_velocity": 4,
    "mortar_splash": 6, "mortar_reload": 5, "mortar_velocity": 4,
}
HEADINGS = (0, 24, -24, 48, -48, 76, -76, 105, -105, 140, -140, 180)

# Change PRESET to compare play styles on the practice leaderboard. "base" is
# the original Fieldline behaviour, unchanged.
PRESET = "base"
PRESETS = {
    #            retreat_hp  retreat_n  loot_reach  zone_lead  gun_range
    "base":     dict(retreat_hp=0.45, retreat_n=3, loot_reach=1.00, zone_lead=10, gun_range=72),
    "cautious": dict(retreat_hp=0.60, retreat_n=2, loot_reach=0.80, zone_lead=14, gun_range=88),
    "greedy":   dict(retreat_hp=0.35, retreat_n=4, loot_reach=1.30, zone_lead=8,  gun_range=60),
}
P = PRESETS[PRESET]


def clamp(number, lower, upper):
    return max(lower, min(upper, number))


def angle_delta(angle, reference):
    return math.remainder(angle - reference, math.tau)


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def direction(angle):
    return math.cos(angle), math.sin(angle)


def bearing(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def aligned_at(me, aim, shot, elapsed):
    yaw, pitch = me["aim"]
    yaw_step = elapsed * me["stats"]["turn_rate"]
    pitch_step = elapsed * me["stats"]["pitch_rate"]
    yaw += clamp(angle_delta(aim[0], yaw), -yaw_step, yaw_step)
    pitch += clamp(aim[1] - pitch, -pitch_step, pitch_step)
    shot_yaw, shot_pitch, tolerance, _ = shot
    return abs(angle_delta(shot_yaw, yaw)) <= tolerance[0] and abs(shot_pitch - pitch) <= tolerance[1]


def laser_charge_goal(values, actor, retreat):
    if retreat or actor.get("hp", math.inf) < values["damage"] * values["min_frac"] * 0.85:
        return values["min_charge"]
    return values["charge"]


def zone_at(obs, time):
    zone = obs["zone"]
    remaining = zone["shrink_end"] - max(obs["t"], zone["shrink_start"])
    blend = 0.0 if time < zone["shrink_start"] else 1.0
    if remaining > 0:
        blend = clamp((time - max(obs["t"], zone["shrink_start"])) / remaining, 0.0, 1.0)
    current, future = zone["center"], zone["next_center"]
    center = tuple(current[i] + (future[i] - current[i]) * blend for i in range(2))
    return center, zone["radius"] + (zone["next_radius"] - zone["radius"]) * blend


def inside_point(pos, center, radius):
    dx, dy = pos[0] - center[0], pos[1] - center[1]
    length = math.hypot(dx, dy)
    multiplier = min(1.0, max(0.0, radius) / length) if length else 0.0
    return center[0] + dx * multiplier, center[1] + dy * multiplier


def target_size(actor, config):
    if actor.get("kind") == "mech":
        factor = 1 + actor.get("cubes", 0) * config["cubes"].get("size_per_cube", 0.05)
        return config["mech"]["radius"] * factor, config["mech"]["height"] * factor
    dimensions = config["npc"] if actor.get("kind") == "npc" else config["crates"]
    return dimensions["radius"], dimensions["height"]


def solve_shot(origin, target, velocity, speed, gravity, spawn_offset):
    """Find the earliest intersection with a constant-velocity target."""
    delta = tuple(b - a for a, b in zip(origin, target))
    if not speed:
        return target, 0.0
    if not gravity:
        # The spawn offset advances the projectile along the resulting aim vector.
        a = sum(v * v for v in velocity) - speed * speed
        b = 2 * (sum(d * v for d, v in zip(delta, velocity)) - speed * spawn_offset)
        c = sum(d * d for d in delta) - spawn_offset * spawn_offset
        discriminant = b * b - 4 * a * c
        if discriminant < 0 or a >= 0:
            return None
        flight = max(0.0, (-b - math.sqrt(discriminant)) / (2 * a))
        return tuple(target[i] + velocity[i] * flight for i in range(3)), flight

    def residual(t):
        dx = delta[0] + velocity[0] * t
        dy = delta[1] + velocity[1] * t
        dz = delta[2] + velocity[2] * t + 0.5 * gravity * t * t
        return math.sqrt(dx * dx + dy * dy + dz * dz) - (speed * t + spawn_offset)

    for step in range(1, 25):
        upper = step * 0.2
        if residual(upper) <= 0:
            lower = upper - 0.2
            for _ in range(12):
                middle = (upper + lower) * 0.5
                if residual(middle) > 0:
                    lower = middle
                else:
                    upper = middle
            return tuple(target[i] + velocity[i] * upper for i in range(3)), upper
    return None


class Bot:
    name = "Fieldline"

    def __init__(self, hello: mf.Hello):
        self.arena = Arena(hello["map"]["dir"])
        self.config = hello["config"]
        self.physics_step = 1 / self.config["match"]["tick_hz"]
        self.decision_time = self.physics_step * self.config["match"]["decide_every"]
        self.side = 1 if hello["seed"] % 2 else -1
        self.last_damage = -100.0
        self.damage_bearing = None
        self.last_attack = -100.0
        self.last_pos = None
        self.last_time = None
        self.stuck = 0.0
        self.moving = False
        self.boosting = False
        self.waypoint = self.route_goal = None
        self.route_time = -100.0
        self.target_id = None

    def weapon_pickup(self, me, pickup):
        kind = pickup["kind"]
        weapons = me["weapons"]
        if kind == "gun" or any(w and w["kind"] == kind for w in weapons):
            return None
        if None in weapons:
            return {}
        # Keep the gun as a dependable option during a specialty weapon's cooldown.
        for index, weapon in enumerate(weapons):
            if weapon["kind"] != "gun" and not weapon.get("charge", 0) and (
                WEAPON_VALUE[kind] > WEAPON_VALUE[weapon["kind"]]
            ):
                return {"drop": index}
        return None

    def update_state(self, obs):
        me, now = obs["self"], obs["t"]
        if self.last_time is not None and now > self.last_time:
            elapsed = min(0.3, now - self.last_time)
            if self.moving and distance(me["pos"], self.last_pos) < elapsed * 1.5:
                self.stuck += elapsed
            else:
                self.stuck = max(0.0, self.stuck - 2 * elapsed)
        self.last_pos, self.last_time = me["pos"], now
        for event in obs["events"]:
            if event["type"] == "damaged" and event["bearing"] is not None:
                self.last_damage, self.damage_bearing = now, event["bearing"]
            elif event["type"] == "hit":
                self.last_attack = now

    def objective(self, obs, threats, pickups, urgent):
        me, now = obs["self"], obs["t"]
        pos, speed = me["pos"], me["stats"]["walk_speed"]
        center, radius = zone_at(obs, now + (17 if urgent else P["zone_lead"]))
        margin = min(28, radius * 0.34)
        safe_spot = inside_point(pos, center, radius - margin)
        if urgent:
            return inside_point(pos, center, radius - min(45, radius * 0.53)), None

        def usable(item, limit, height=4.0):
            destination = item["pos"]
            travel = distance(pos, destination)
            if travel > limit or abs(pos[2] - destination[2]) > height:
                return False
            future_center, future_radius = zone_at(obs, now + travel / speed + 8)
            return distance(destination, future_center) + min(12, future_radius * 0.35) <= future_radius

        if threats and threats[0][0] < 34:
            return safe_spot, None
        options = []
        first_weapon_bonus = 18 if None in me["weapons"] else 0
        for item, _ in pickups:
            if usable(item, 95 * P["loot_reach"]):
                priority = 42 + first_weapon_bonus + 4 * WEAPON_VALUE[item["kind"]]
                options.append((priority - distance(pos, item["pos"]), item))
        for item in obs["cubes"]:
            if usable(item, 88 * P["loot_reach"]):
                options.append((67 - distance(pos, item["pos"]), item))
        for drop in obs.get("drops", ()):
            if drop["lands_in"] <= 2 and drop["holder"] in (None, me["id"]) and usable(drop, 90):
                missing_hp = 1 - me["hp"] / me["max_hp"]
                options.append((74 + 44 * missing_hp - distance(pos, drop["pos"]), drop))
        if options:
            priority, item = max(options, key=lambda entry: entry[0])
            if priority > 0:
                return item["pos"], item
        if distance(pos, safe_spot) > 6:
            return safe_spot, None
        if not threats and obs["alive"] > 3 and radius > 45:
            crates = [item for item in obs["crates"] if usable(item, 82, 5)]
            if crates:
                crate = min(crates, key=lambda item: distance(pos, item["pos"]))
                direction_to_crate = direction(bearing(crate["pos"], pos))
                stand = tuple(crate["pos"][i] + 15 * direction_to_crate[i] for i in range(2))
                return stand, crate
        if distance(pos, center) > radius * 0.55 and not threats:
            return inside_point(pos, center, radius * 0.54), None
        return pos, None

    def movement(self, obs, goal, threats, retreat, urgent):
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        width, height, step_up = stats["radius"] + 0.25, stats["height"], stats["step_up"]
        travel = distance(pos, goal)
        can_route = travel > 10 and (urgent or not threats or threats[0][0] > 40)
        if can_route and now - self.route_time >= 0.65 and (
            self.waypoint is None or distance(goal, self.route_goal) > 13
            or distance(pos, self.waypoint) < 3 or self.stuck > 0.5 or now - self.route_time >= 2.5
        ):
            self.route_time, self.route_goal = now, goal
            probe = min(25.0, travel)
            direct = direction(bearing(pos, goal))
            if self.arena.walk_clearance(pos, direct, width, height, step_up, probe) >= probe:
                self.waypoint = goal
            else:
                self.waypoint = self.arena.next_waypoint(
                    pos, goal, width, height, step_up, deadline=perf_counter() + 0.006,
                )
        waypoint = goal
        if can_route and self.waypoint is not None and distance(goal, self.route_goal) < 13:
            if distance(pos, self.waypoint) > 2.4:
                waypoint = self.waypoint

        yaw = bearing(pos, waypoint)
        close = threats[0] if threats and threats[0][0] < 109 else None
        if close and not urgent:
            gap, threat = close
            enemy_yaw = bearing(pos, threat["pos"])
            lightning = next((w for w in me["weapons"] if w and w["kind"] == "lightning"), None)
            preferred = 46 if lightning else P["gun_range"]
            isolated = len(threats) == 1 or threats[1][0] > 85
            if lightning and isolated and me["energy"] >= max(18, 5 * lightning["stats"]["energy"]):
                preferred = 0.74 * lightning["stats"]["range"]
            if retreat:
                yaw = enemy_yaw + math.pi + self.side * 0.35
            elif travel < 8 or gap < 58:
                correction = clamp((preferred - gap) / 55, -0.54, 0.96)
                yaw = enemy_yaw + self.side * (math.pi / 2 + correction)
        elif retreat and not urgent and self.damage_bearing is not None:
            yaw = self.damage_bearing + math.pi + self.side * 0.35

        pos_x, pos_y, pos_z = pos
        near_projectiles = []
        for projectile in obs["projectiles"]:
            px, py, pz = projectile["pos"]
            vx, vy, _ = projectile["vel"]
            dx, dy = pos_x - px, pos_y - py
            if dx * dx + dy * dy < 85**2 and dx * vx + dy * vy > 0:
                near_projectiles.append(projectile)
        near_projectiles.sort(key=lambda projectile: distance(pos, projectile["pos"]))
        near_projectiles = near_projectiles[:12]
        if travel < 2.2 and not close and not retreat and not near_projectiles:
            return [0.0, 0.0], False, False

        center, radius = zone_at(obs, now + 2.0)
        ahead = stats["walk_speed"] * 0.85
        best = None
        best_score, longest = -math.inf, 0.0
        for offset in HEADINGS:
            vector = direction(yaw + self.side * math.radians(offset))
            clear = self.arena.walk_clearance(pos, vector, width, height, step_up, ahead)
            longest = max(longest, clear)
            end_x, end_y = pos_x + vector[0] * clear, pos_y + vector[1] * clear
            zone_distance = math.hypot(end_x - center[0], end_y - center[1])
            zone_cost = max(0.0, zone_distance - radius + min(3, radius * 0.25))
            score = math.cos(math.radians(offset)) * 4.0 + 0.67 * clear - 1.4 * zone_cost
            if urgent:
                score -= 0.19 * zone_distance
            if retreat and close and not urgent:
                score += 0.24 * distance((end_x, end_y), close[1]["pos"])
            for projectile in near_projectiles:
                px, py, pz = projectile["pos"]
                vx, vy, vz = projectile["vel"]
                speed = stats["walk_speed"] if clear > 3 else 0.0
                dx, dy = px - pos_x, py - pos_y
                vx, vy = vx - vector[0] * speed, vy - vector[1] * speed
                approach = -(dx * vx + dy * vy) / max(vx * vx + vy * vy, 1)
                if 0 < approach < 0.55 and pos_z - 3 < pz + vz * approach < pos_z + height + 3:
                    miss = math.hypot(dx + vx * approach, dy + vy * approach)
                    danger_radius = width + (6.5 if projectile["kind"] == "mortar" else 1.7)
                    score -= 2.2 * max(0.0, danger_radius - miss)
            if score > best_score:
                best_score, best = score, (vector, clear)

        vector, clear = best
        detour = self.stuck > 0.75 and (urgent or longest < 3.2)
        jump = detour and me["grounded"] and me["energy"] > 35
        if detour:
            vector, clear = direction(yaw), ahead
        if not me["grounded"]:
            boost = me["energy"] > 18 and (detour or clear < 2.5) and travel > 10
        else:
            reserve = 10 if urgent else 31
            enough_energy = me["energy"] > reserve + (0 if self.boosting else 20)
            boost = enough_energy and clear > 4.5 and (urgent or retreat or bool(near_projectiles))
        speed = 1.0 if close or retreat or urgent or travel >= 6 else clamp(travel / 6, 0.35, 1.0)
        return [speed * vector[0], speed * vector[1]], boost, jump

    def firing_time(self, weapon):
        # A cooling weapon can start firing after the torso has turned one more tick.
        wait = max(weapon["cooldown"], weapon.get("reload", 0.0))
        if weapon["kind"] != "laser" and self.physics_step + 1e-6 < wait < self.decision_time:
            return self.physics_step * math.ceil((wait - 1e-6) / self.physics_step)
        return self.physics_step

    def shot(self, me, actor, weapon):
        stats, config = me["stats"], self.config
        kind = weapon["kind"]
        values = {**config["weapons"][kind], **weapon["stats"]}
        radius, height = target_size(actor, config)
        origin = tuple(me["pos"])
        muzzle = stats["height"] + 0.5 if kind == "mortar" else stats["muzzle_height"]
        origin = origin[0], origin[1], origin[2] + muzzle
        velocity = actor.get("vel", (0, 0, 0))
        elapsed = self.firing_time(weapon)
        target = tuple(actor["pos"][i] + elapsed * velocity[i] for i in range(3))
        target = target[0], target[1], target[2] + 0.54 * height
        gap = math.dist(origin, target)
        if gap > values.get("range", 206) or (kind == "mortar" and gap < 21):
            return None
        gravity = values.get("gravity", 0.0)
        result = solve_shot(origin, target, velocity, values.get("speed", 0), gravity, stats["radius"] + 0.5)
        if result is None:
            return None
        endpoint, flight = result
        horizontal = distance(origin, endpoint)
        pitch = math.atan2(endpoint[2] - origin[2] + 0.5 * gravity * flight * flight, horizontal)
        if not stats["min_pitch"] <= pitch <= stats["max_pitch"]:
            return None
        if not self.arena.clear_shot(origin, endpoint, gravity, flight):
            return None
        yaw = bearing(origin, endpoint)
        if kind == "lightning":
            tolerance = math.radians(values["cone_deg"] * 0.46), math.pi
        else:
            tolerance = (
                math.atan2(radius * 0.84, max(1.0, horizontal)),
                math.atan2(height * 0.38, max(1.0, horizontal)),
            )
        return yaw, pitch, tolerance, values

    def attack(self, obs, threats, objective, retreat, deadline):
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        if retreat:
            threats = [entry for entry in threats if entry[0] <= 42 or entry[1].get("hp", 1400) <= 155]
        actors = [actor for _, actor in threats]
        npcs = [actor for actor in obs["mechs"] if actor["kind"] == "npc" and distance(pos, actor["pos"]) < 52]
        healing = me["hp"] < me["max_hp"] * 0.72 and now - max(self.last_damage, self.last_attack) < 10
        if not actors and not retreat and not healing and now - self.last_damage > 2:
            actors.extend(npcs)
            if objective and objective["id"] < 5000 and "hp" in objective:
                actors.append(objective)
        actors.sort(key=lambda actor: (
            distance(pos, actor["pos"]) * 0.56
            + actor.get("hp", 1200) * (0.041 if actor.get("kind") == "mech" else 0.027)
            + 8 * abs(angle_delta(bearing(pos, actor["pos"]), me["aim"][0]))
            - (5 if actor["id"] == self.target_id else 0)
        ))

        weapons = me["weapons"]
        for actor in actors[:6]:
            if perf_counter() >= deadline:
                break
            plans = [self.shot(me, actor, weapon) if weapon else None for weapon in weapons]
            if not any(plans):
                continue
            priorities = []
            for index, plan in enumerate(plans):
                if plan is None:
                    continue
                weapon, values = weapons[index], plan[3]
                energy_ok = me["energy"] >= values.get("energy", 0) + (17 if retreat else 0)
                loaded = weapon["kind"] != "gun" or weapon.get("ammo", 0) > 0
                ready = weapon["cooldown"] < self.decision_time and weapon.get("reload", 0) < self.decision_time
                charging = weapon.get("charge", 0) > 0
                priorities.append((charging, ready and energy_ok and loaded, WEAPON_VALUE[weapon["kind"]], index))
            charging, _, _, primary = max(priorities)
            laser_slot = primary if charging else None
            if laser_slot is not None:
                laser_plan = plans[laser_slot]
                laser_goal = laser_charge_goal(laser_plan[3], actor, retreat)
                gun_slot = 1 - laser_slot
                gun, gun_plan = weapons[gun_slot], plans[gun_slot]
                if (
                    laser_goal - weapons[laser_slot]["charge"] > self.decision_time + 1e-6
                    and gun_plan is not None and gun["kind"] == "gun" and gun.get("ammo", 0) > 0
                    and max(gun["cooldown"], gun.get("reload", 0)) < self.decision_time
                    and abs(angle_delta(gun_plan[0], laser_plan[0])) < self.physics_step * stats["turn_rate"]
                    and abs(gun_plan[1] - laser_plan[1]) < self.physics_step * stats["pitch_rate"]
                    and aligned_at(me, gun_plan[:2], gun_plan, self.firing_time(gun))
                ):
                    primary = gun_slot
            yaw, pitch = plans[primary][:2]
            fire = [False, False]
            for index, plan in enumerate(plans):
                if plan is None:
                    continue
                values = plan[3]
                weapon = weapons[index]
                aligned = aligned_at(me, (yaw, pitch), plan, self.firing_time(weapon))
                if weapon["kind"] == "laser":
                    charge = weapon.get("charge", 0.0)
                    target_charge = laser_charge_goal(values, actor, retreat)
                    fire[index] = (index == primary or index == laser_slot) and weapon["cooldown"] < 0.08 and (
                        charge > 0 or abs(angle_delta(yaw, me["aim"][0])) < 0.48
                    ) and not (aligned and charge >= target_charge - 0.025)
                else:
                    fire[index] = aligned and max(weapon["cooldown"], weapon.get("reload", 0)) < self.decision_time and (
                        me["energy"] >= values.get("energy", 0) + (17 if retreat else 0)
                    )
            self.target_id = actor["id"]
            return [yaw, pitch], fire
        self.target_id = None
        if now - self.last_damage < 2.0 and self.damage_bearing is not None:
            return [self.damage_bearing, 0.0], [False, False]
        if threats:
            return [bearing(pos, threats[0][1]["pos"]), 0.0], [False, False]
        nearby = [sound for sound in obs["sounds"] if sound["dist"] == "near" and sound["age"] <= 0.25
                  and sound["kind"] in ("gun", "laser_charge", "laser", "lightning", "mortar", "boost", "jet")]
        if nearby:
            return [min(nearby, key=lambda sound: sound["age"])["bearing"], 0.0], [False, False]
        return None, [False, False]

    def act(self, obs: mf.Observation) -> mf.Action:
        deadline = perf_counter() + self.config["bot"]["tick_budget_ms"] * 0.00058
        self.update_state(obs)
        me, now = obs["self"], obs["t"]
        pos, health = me["pos"], me["hp"] / me["max_hp"]
        threats = sorted(
            ((distance(pos, actor["pos"]), actor) for actor in obs["mechs"] if actor["kind"] == "mech"),
            key=lambda entry: entry[0],
        )
        pickups = [(item, self.weapon_pickup(me, item)) for item in obs["pickups"]]
        pickups = [(item, swap) for item, swap in pickups if swap is not None]
        center, radius = zone_at(obs, now + 8)
        urgent = not me["in_zone"] or distance(pos, center) > radius - min(7, radius * 0.28)
        under_fire = now - self.last_damage < 3.0
        close_count = sum(gap < 75 for gap, _ in threats)
        retreat = (health < P["retreat_hp"] and (under_fire or close_count)) or close_count >= P["retreat_n"]
        goal, objective = self.objective(obs, threats, pickups, urgent)
        move, boost, jump = self.movement(obs, goal, threats, retreat, urgent)
        aim, fire = self.attack(obs, threats, objective, retreat, deadline)
        if aim is None:
            heading = bearing(pos, goal) if distance(pos, goal) > 3 else 0.37 * now + 0.4 * me["id"]
            aim = [heading + 0.65 * math.sin(now * 1.2), 0.0]
        self.moving = math.hypot(*move) >= 0.7
        self.boosting = boost
        action: mf.Action = {"tick": obs["tick"], "move": move, "boost": boost, "jump": jump, "aim": aim, "fire": fire}
        for item, swap in sorted(pickups, key=lambda entry: -WEAPON_VALUE[entry[0]["kind"]]):
            if distance(pos, item["pos"]) <= self.config["loot"]["pickup_radius"] and abs(pos[2] - item["pos"][2]) <= 4:
                action.update(take=item["id"], **swap)
                break
        offer = obs["offer"]
        if offer and offer["choices"]:
            action["pick"] = max(
                range(len(offer["choices"])),
                key=lambda index: UPGRADE_VALUE.get(offer["choices"][index]["id"], 0),
            )
        if not under_fire and not any(fire):
            action["reload"] = any(
                weapon and weapon["kind"] == "gun" and weapon.get("reload", 0) == 0
                and weapon.get("ammo", math.inf) <= 4 for weapon in me["weapons"]
            )
        return action


if __name__ == "__main__":
    mf.run(Bot)

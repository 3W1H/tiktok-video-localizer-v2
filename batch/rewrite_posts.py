#!/usr/bin/env python
"""Rewrite tiktok-post.md copy per video instead of the batch template.

WHY (2026-11-15, user feedback)
-------------------------------
auto_decide.py lines 492-528 built every clip's post copy from ONE template:

    understanding.summary = "%s - %s. Erase-only localization." % (headline, stat)
    post.caption         = "%s. %s. Clean cut of the original footage - ..."
    post.hashtags        = ["NBA","Basketball","NBAClassics","HoopDynastyDaily",
                            re.sub(r"\\W","",headline)[:22]]
    post.pinned_comment  = "More NBA classics daily ... Which play is your favourite? ..."

Only the cover headline changed, so 36/40 posts were structurally identical:
4 of 5 hashtags appeared in 90-100% of posts and the 5th was a made-up
all-caps string that nobody searches for. The summary also carried
"Erase-only localization." -- a production note, explicitly forbidden by
SKILL.md rule 1b ("Summary only describes the video content itself").

This script is the per-video copy source of truth. Every entry below was
written from the actual source filename plus the clip's OCR / ASR evidence
(see diag/post-content-facts.json), NOT from the cover headline. Notably
idx03 is a Kyrie Irving Nets handles reel (the template called it
"NBA CLASSICS") and idx00 is a Kobe memorial tribute (not a happy montage).

For each clip we update work/post-content.json + work/video-understanding.json
and re-render the full nine-section markdown with the skill's own
generate_post.py, so BGM credit, posting time, cover and processing report
stay intact and authoritative.

Excluded by the user: idx01 / idx02 (their -done folders).
Excluded as already hand-authored: idx04 / idx10 / idx17 / idx41.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.environ.get("LOCALIZER_BATCH_ROOT", os.path.dirname(os.path.abspath(__file__)))
SK = os.environ.get("LOCALIZER_SKILL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/scripts"
PY = os.environ.get("LOCALIZER_PYTHON", sys.executable)
sys.path.insert(0, ROOT)
import run_queue as Q  # noqa: E402

NL = chr(10)
MANUAL = (4, 10, 17, 41)          # already personal, keep verbatim
EXCLUDED = (1, 2)                 # user's -done clips

# idx -> per-video English copy. caption / pinned are lists of paragraphs.
COPY = {
    0: {
        "summary": "A tribute cut built around Kobe Bryant's passing: archive footage of Kobe with Shaq, LeBron, Curry and Dwyane Wade, fan reactions and a Jay Chou song.",
        "caption": [
            "The day the game went quiet. A Kobe tribute cut - archive clips, reactions from LeBron, Curry and Wade, and a Jay Chou song that still hits different.",
            "Which Kobe moment always gets you?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#MambaMentality", "#NBALegends", "#Lakers", "#JayChou"],
        "pinned": [
            "More Kobe archive drops this week - the 2009 ring, the 81-point night and the farewell. Follow @HoopDynastyDaily.",
            "Where were you when you heard the news?",
        ],
    },
    3: {
        "summary": "Kyrie Irving's Brooklyn Nets highlight reel: ankle-breaking crossovers, tight-space dribbles and soft finishes at the rim against NBA defences.",
        "caption": [
            "The handles are genuinely unfair. Kyrie in Brooklyn - every crossover, every wrong-footed defender, every impossible finish at the rim.",
            "Best ball-handler of this generation: Kyrie or someone else?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KyrieIrving", "#BrooklynNets", "#NBAHandles", "#Crossover", "#Basketball"],
        "pinned": [
            "Next: a 7-footer with guard handles and a Curry finishing package. Follow @HoopDynastyDaily for the handles series.",
            "Which Kyrie move would you steal for your own game?",
        ],
    },
    5: {
        "summary": "Kobe Bryant scores 61 at Madison Square Garden against the Knicks: 19-of-31 shooting, a perfect 20-for-20 at the line, and the home crowd chanting MVP.",
        "caption": [
            "61 at the Garden. 31 shots, 19 makes, 20-for-20 from the stripe - and Knicks fans chanting MVP for the visiting guy. February 2009 Kobe was untouchable.",
            "Better road performance: 61 at MSG or 81 vs Toronto?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#MadisonSquareGarden", "#Lakers", "#NBAHistory", "#MambaMentality"],
        "pinned": [
            "Next in the Kobe archive: the 81-point night and the 2005-06 scoring title run. Follow @HoopDynastyDaily.",
            "What's the most disrespectful away-game stat line you've seen?",
        ],
    },
    6: {
        "summary": "Dirk Nowitzki and the 2011 Dallas Mavericks erase a 15-point deficit against the Miami Heat, the comeback that launched their championship run.",
        "caption": [
            "Down 15 in the fourth. One Dirk one-legged fadeaway at a time. The 2011 Mavs comeback that changed how we remember that Finals.",
            "Was 2011 Dirk the toughest title run ever?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#DirkNowitzki", "#DallasMavericks", "#NBAFinals", "#2011Finals", "#NBAHistory"],
        "pinned": [
            "More one-man-changed-a-franchise tapes coming - KG, Nash, Wade. Follow @HoopDynastyDaily.",
            "Which 2011 Mavericks moment do you replay?",
        ],
    },
    7: {
        "summary": "The 20th-anniversary look back at Kobe Bryant's 81-point night against the Toronto Raptors, the second-highest single-game total in NBA history.",
        "caption": [
            "81 points. Twenty years later and it still doesn't look real. Every make from the night Kobe took apart the Raptors.",
            "Where does 81 rank in your all-time single-game list?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#81Points", "#Lakers", "#TorontoRaptors", "#NBAHistory"],
        "pinned": [
            "Next: the 61 at MSG and the 35.4 PPG season that came with it. Follow @HoopDynastyDaily.",
            "Could anyone today drop 81 in a real game?",
        ],
    },
    8: {
        "summary": "Prime Derrick Rose in Chicago: the MVP-era burst, sudden changes of direction and fearless rim attacks that made him too big, too strong and too fast.",
        "caption": [
            "Too big. Too strong. Too fast. Too good. Prime D-Rose in Chicago - the youngest MVP ever, playing like the rim owed him money.",
            "What's your favourite 2011 Rose moment?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#DerrickRose", "#ChicagoBulls", "#NBAMVP", "#NBA", "#BasketballHighlights"],
        "pinned": [
            "Next up: prime Wade and the Vince Carter Toronto era. Follow @HoopDynastyDaily for the 2000s archive.",
            "If Rose stays healthy, how many rings do the Bulls win?",
        ],
    },
    9: {
        "summary": "A compilation of alley-oop finishes: lob passes from all over the court turned into dunks by Blake Griffin, Kevin Durant and company.",
        "caption": [
            "The ball barely touches human hands. A full reel of lobs, timing and ridiculous in-air finishes from around the league.",
            "Which duo had the best lob connection ever?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#AlleyOop", "#NBADunks", "#Basketball", "#NBAHighlights", "#LobCity"],
        "pinned": [
            "More connection reels on the way - LeBron and Wade, Stockton and Malone. Follow @HoopDynastyDaily.",
            "Who throws the prettiest lob in NBA history?",
        ],
    },
    11: {
        "summary": "Payton Pritchard's 2025-26 Celtics mixtape: a small guard using pace, angles and a quick release to punish bigger defenders.",
        "caption": [
            "Small guard, big problem. Pritchard's 25-26 tape is a masterclass in using angles, pace and a quick release against size.",
            "Best small-on-big guard in the league right now?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#PaytonPritchard", "#BostonCeltics", "#NBA", "#PointGuard", "#BasketballIQ"],
        "pinned": [
            "More guard craft coming - Kyrie handles, KD's 7-foot handle package, Curry's rim finishing. Follow @HoopDynastyDaily.",
            "Which small guard gives bigs the hardest night?",
        ],
    },
    12: {
        "summary": "Kevin Durant's ball-handling reel: a seven-foot frame, guard-level crossovers and between-the-legs sequences ending in pull-ups or drives to the rim.",
        "caption": [
            "Seven feet tall. Guard handles. KD takes the ball up the floor, shakes a defender with a crossover and rises into a pull-up like it's routine.",
            "Best handler over 6-foot-10 - KD or someone else?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KevinDurant", "#NBAHandles", "#NBAScoring", "#Basketball", "#NBASkills"],
        "pinned": [
            "Next: a Curry finish package and a Kyrie handles tape. Follow @HoopDynastyDaily for the skills series.",
            "What's the smoothest KD combo you've seen?",
        ],
    },
    13: {
        "summary": "Kobe Bryant leads the 2002 Lakers back from a 27-point fourth-quarter deficit against the Dallas Mavericks in a classic road comeback.",
        "caption": [
            "Down 27 to start the fourth in Dallas. Kobe took the game personally. The full 2002 comeback, possession by possession.",
            "Greatest single-quarter takeover you've ever watched?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#Lakers", "#DallasMavericks", "#NBAComeback", "#NBAHistory"],
        "pinned": [
            "More comeback tapes next - Dirk 2011 and T-Mac's 35 seconds. Follow @HoopDynastyDaily.",
            "Was this Kobe's most underrated fourth quarter?",
        ],
    },
    14: {
        "summary": "All-Star Weekend's most memorable Shaquille O'Neal moments: the big man joking, dancing and overpowering the league's best in exhibition play.",
        "caption": [
            "All-Star Shaq was a whole different character. The dunks, the jokes, the sheer size difference - every iconic moment in one reel.",
            "Which All-Star Shaq moment lives in your head rent-free?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#ShaquilleONeal", "#NBAAllStar", "#Lakers", "#NBA", "#NBALegends"],
        "pinned": [
            "Next: prime Shaq in the paint and the night he bought his own tickets. Follow @HoopDynastyDaily.",
            "Funniest All-Star big man ever?",
        ],
    },
    15: {
        "summary": "Kevin Garnett's ten best career plays, from Minnesota Timberwolves dominance to the Boston Celtics championship era.",
        "caption": [
            "Ten plays. Maximum intensity. KG's career from Minnesota to the 2008 Celtics, in full scream-at-the-camera glory.",
            "Which KG era was better - Wolves or Celtics?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KevinGarnett", "#MinnesotaTimberwolves", "#BostonCeltics", "#NBATopPlays", "#NBA"],
        "pinned": [
            "More career top-10s queued up - Kobe, Nash, Wemby. Follow @HoopDynastyDaily.",
            "Where does KG rank among all-time power forwards?",
        ],
    },
    16: {
        "summary": "Vince Carter's Toronto Raptors era: the arrival of Half Man Half Amazing and the run of dunks that turned a franchise into a league-wide obsession.",
        "caption": [
            "The night Toronto fell in love with basketball. Vince Carter in the Raptors era - every windmill, every hang-time moment, every packed arena.",
            "Best dunker ever: Vince or someone else?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#VinceCarter", "#TorontoRaptors", "#NBADunks", "#NBAHistory", "#AirCanada"],
        "pinned": [
            "Next: the Raptors-era aerial reel and the 2000 dunk contest. Follow @HoopDynastyDaily.",
            "Which Vince dunk is still the most impossible?",
        ],
    },
    18: {
        "summary": "Dennis Rodman's most unpredictable moments: the rebounding, the deflections, the hair, and the on-court behaviour nobody could read.",
        "caption": [
            "Nobody played the game like The Worm. Rodman chasing every loose ball, arguing with everyone, and somehow making it all work.",
            "Best rebounder you've ever seen?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#DennisRodman", "#ChicagoBulls", "#NBARebounding", "#NBADefense", "#NBALegends"],
        "pinned": [
            "More 90s Bulls tapes next - Jordan vs Malone, the 97-98 Finals. Follow @HoopDynastyDaily.",
            "Would Rodman survive in today's NBA?",
        ],
    },
    19: {
        "summary": "Vince Carter's Raptors-era aerial reel: above-the-rim dunks, in-flight adjustments and the hang time that defined Air Canada.",
        "caption": [
            "Takeoff, hang, finish. A full reel of Vince Carter in Toronto doing things gravity clearly didn't approve.",
            "Which Raptors-era Vince dunk would you put number one?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#VinceCarter", "#TorontoRaptors", "#SlamDunk", "#NBADunks", "#NBA"],
        "pinned": [
            "Next: the 2007 dunk contest tape with Jordan and Kobe on the judges' panel. Follow @HoopDynastyDaily.",
            "Greatest in-game dunk of all time - go.",
        ],
    },
    20: {
        "summary": "Dwyane Wade's 20 best plays from the 2008-09 Miami Heat season: the scoring-title year built on slashes, euro-steps and chase-down blocks.",
        "caption": [
            "08-09 Flash was a one-man fast break. Twenty plays from Wade's scoring-title season - every slash, every euro-step, every impossible finish.",
            "Best scoring guard season of the 2000s?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#DwyaneWade", "#MiamiHeat", "#NBA", "#NBAScoringChampion", "#BasketballHighlights"],
        "pinned": [
            "Next: the LeBron and Wade connection reel. Follow @HoopDynastyDaily.",
            "Where does 08-09 Wade rank among all-time guard seasons?",
        ],
    },
    21: {
        "summary": "Shaquille O'Neal's career night against the Clippers, sparked by the story that he had to buy tickets for his own family on his birthday.",
        "caption": [
            "They made Shaq buy tickets for his own birthday. He responded with the most dominant game of his career. Never poke the big fella.",
            "Angriest you-made-this-personal game you remember?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#ShaquilleONeal", "#Lakers", "#LosAngelesClippers", "#NBADominance", "#NBAHistory"],
        "pinned": [
            "More prime-Shaq tapes next - the All-Star moments and the paint-dominance cut. Follow @HoopDynastyDaily.",
            "What's the pettiest reason an athlete ever went off?",
        ],
    },
    22: {
        "summary": "Stephen Curry's finishing package: changes of pace, soft touch at the rim and layup angles that make him as efficient inside as he is from three.",
        "caption": [
            "Everyone talks about the range. Watch the finishes. Curry's rhythm, body control and soft touch make him one of the most efficient rim finishers at his size.",
            "Most underrated part of Curry's game?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#StephenCurry", "#GoldenStateWarriors", "#NBAFinishing", "#BasketballIQ", "#NBA"],
        "pinned": [
            "Next: the 2016 OKC game-winner and Kyrie's handle tape. Follow @HoopDynastyDaily.",
            "Which guard has the best touch around the rim?",
        ],
    },
    24: {
        "summary": "Chicago Bulls vs Utah Jazz Finals footage: Jordan, Malone, Rodman and Stockton trading possessions, with Phil Jackson's late-game timeouts and free-throw sequences.",
        "caption": [
            "Bulls. Jazz. Every possession of a Finals classic - Jordan and Malone going shot for shot, Stockton late, Phil Jackson burning timeouts.",
            "97 or 98 - which Finals was better?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#MichaelJordan", "#KarlMalone", "#ChicagoBulls", "#UtahJazz", "#NBAFinals"],
        "pinned": [
            "More 90s Finals film on the way - the Rodman reel and the 97-98 run. Follow @HoopDynastyDaily.",
            "Who had the tougher job: guarding Jordan or guarding Malone?",
        ],
    },
    26: {
        "summary": "Victor Wembanyama bending the idea of a big man: perimeter shooting, switchable defence and rim protection from a 7-foot-4 frame.",
        "caption": [
            "At 7-foot-4, pulling up from three, then erasing a shot at the rim on the next possession. Wemby isn't a new type of big - he's a new type of player.",
            "What's the ceiling here, honestly?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#Wembanyama", "#SanAntonioSpurs", "#NBA", "#NextGenNBA", "#Basketball"],
        "pinned": [
            "Next: the Wemby career top-15 reel. Follow @HoopDynastyDaily.",
            "Which Wemby skill is the most unguardable?",
        ],
    },
    27: {
        "summary": "Kobe Bryant's farewell game: 60 points against the Utah Jazz on the final night of his career, revisited ten years on.",
        "caption": [
            "60 points. On the last night. Against Utah. Ten years later, Mamba Out still gives chills.",
            "Where were you for the farewell game?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#MambaOut", "#Lakers", "#60Points", "#NBAHistory"],
        "pinned": [
            "Next: the pre-game tributes and the 8/24 jersey retirement speech. Follow @HoopDynastyDaily.",
            "Best farewell game in sports history - agree or not?",
        ],
    },
    28: {
        "summary": "The 2007 NBA Dunk Contest revisited, with Michael Jordan, Kobe Bryant and Vince Carter among the all-time greats watching from the judges' table.",
        "caption": [
            "The dunks were fine. The judging panel was Jordan, Kobe and Vince Carter. A 2007 contest worth rewatching for the reactions alone.",
            "Which dunk contest had the best lineup ever?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#NBADunkContest", "#NBAAllStar", "#MichaelJordan", "#KobeBryant", "#VinceCarter"],
        "pinned": [
            "Next: the Vince Carter Raptors dunk reels. Follow @HoopDynastyDaily.",
            "Best dunk contest champion of all time?",
        ],
    },
    29: {
        "summary": "The Lakers retire both Kobe Bryant's No. 8 and No. 24 jerseys, with his speech about effort, legacy and what the numbers meant.",
        "caption": [
            "Two numbers, one career. The night the Lakers raised 8 and 24 - and Kobe turned the speech into a lesson about work.",
            "Which number is your Kobe?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#Lakers", "#JerseyRetirement", "#MambaMentality", "#NBALegends"],
        "pinned": [
            "Next: the farewell-game tributes and the 60-point night. Follow @HoopDynastyDaily.",
            "8 or 24 - pick one and defend it.",
        ],
    },
    30: {
        "summary": "Victor Wembanyama's fifteen best plays so far, counted down from his early Spurs career to his most recent blocks and perimeter scores.",
        "caption": [
            "Fifteen plays, one impossible frame. A counted-down Wemby reel - blocks nobody else reaches, threes nobody his size should take.",
            "Which play should be number one?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#Wembanyama", "#SanAntonioSpurs", "#NBATopPlays", "#NBA", "#Basketball"],
        "pinned": [
            "Next: the Wemby skill breakdown cut. Follow @HoopDynastyDaily.",
            "Where does Wemby rank in today's NBA right now?",
        ],
    },
    31: {
        "summary": "Tracy McGrady's athletic prime across Orlando and Houston: the first step, the lift on his pull-up and the effortless scoring form.",
        "caption": [
            "T-Mac made 30 look effortless. The first step, the elevation, the wrist snap - a body built for scoring and a game to match.",
            "Most naturally gifted wing ever?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#TracyMcGrady", "#OrlandoMagic", "#HoustonRockets", "#NBA", "#NBALegends"],
        "pinned": [
            "Next: the 35-second, 13-point miracle against San Antonio. Follow @HoopDynastyDaily.",
            "Prime T-Mac or prime Kobe - who's harder to guard?",
        ],
    },
    32: {
        "summary": "A short edit comparing how Kobe Bryant matched up with LeBron James versus the rest of the league, built from their head-to-head footage.",
        "caption": [
            "Kobe guarded LeBron differently. Different stance, different attention, different energy - a short cut on the matchup that always felt personal.",
            "Who got the better of the head-to-head?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#LeBronJames", "#NBARivalries", "#Lakers", "#NBA"],
        "pinned": [
            "Next: the heavy-guard isolation cut with both of them. Follow @HoopDynastyDaily.",
            "Did we ever get the Finals matchup we wanted?",
        ],
    },
    33: {
        "summary": "Part one of Kobe Bryant's career top-ten plays, opening a four-part countdown of his most memorable finishes and clutch shots.",
        "caption": [
            "Part 1 of the Kobe top-ten countdown. Start with the ones everyone remembers, then argue about the order.",
            "What has to be number one?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#NBATopPlays", "#Lakers", "#MambaMentality", "#NBA"],
        "pinned": [
            "Parts 2-4 are coming - follow @HoopDynastyDaily so you don't miss the rest of the countdown.",
            "Which Kobe play absolutely must be #1?",
        ],
    },
    34: {
        "summary": "The tribute reel aired before Kobe Bryant's final game: players, coaches and former teammates including Derek Fisher sending their regards.",
        "caption": [
            "Before the 60 points, the league said goodbye. Jordan, Fisher, teammates and rivals on what Kobe meant to them.",
            "Which tribute hit you hardest?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#Lakers", "#MambaMentality", "#NBAHistory", "#NBALegends"],
        "pinned": [
            "Next: the 60-point farewell game itself. Follow @HoopDynastyDaily.",
            "Who gave the best Kobe tribute?",
        ],
    },
    35: {
        "summary": "Steve Nash's ten greatest assists: no-look passes, pick-and-roll reads and full-court dimes from the Phoenix Suns' Seven Seconds or Less era.",
        "caption": [
            "Ten dimes from the guy who made passing the whole show. Nash bending a defence with a look, an angle and a touch pass.",
            "Best pure passer you've ever watched?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#SteveNash", "#PhoenixSuns", "#NBAAssists", "#PointGuard", "#NBAHistory"],
        "pinned": [
            "Next: more career top-10s - KG, Kobe, Wemby. Follow @HoopDynastyDaily.",
            "Which Nash pass is still impossible to explain?",
        ],
    },
    36: {
        "summary": "The 2016 Thunder-Warriors classic: Stephen Curry forces overtime and buries the long game-winner in a Saturday primetime finish.",
        "caption": [
            "Overtime. 107-108. Curry from way downtown. The 2016 OKC-Warriors game that felt like the regular season stopped mattering.",
            "Best regular-season game you've ever watched?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#StephenCurry", "#GoldenStateWarriors", "#OKCThunder", "#NBAClassics", "#NBA"],
        "pinned": [
            "Next: the Curry rim-finishing tape. Follow @HoopDynastyDaily.",
            "Where does that shot rank among Curry's career moments?",
        ],
    },
    37: {
        "summary": "A look at Ja Morant's career so far: the vertical explosion, the leap over Wembanyama, and the off-court controversies alongside the highlights.",
        "caption": [
            "The talent and the noise, in one reel. Ja's absurd lift - including the leap over a 7-foot-4 Wemby - alongside everything that came with it.",
            "What's the most ridiculous Ja finish?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#JaMorant", "#MemphisGrizzlies", "#NBADunks", "#NBA", "#Basketball"],
        "pinned": [
            "Next: more modern-era reels - Wemby and Curry. Follow @HoopDynastyDaily.",
            "Peak Ja: where would he rank if nothing else happened?",
        ],
    },
    38: {
        "summary": "LeBron James and Dwyane Wade's best two-man connections in Miami, from Wade's full-court quarterback throws to bounce-pass finishes at the rim.",
        "caption": [
            "Wade throws it three-quarters of the court, LeBron finishes before the defence turns around. The full connection reel from the Miami years.",
            "Best duo chemistry ever - yes or no?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#LeBronJames", "#DwyaneWade", "#MiamiHeat", "#AlleyOop", "#NBA"],
        "pinned": [
            "Next: 08-09 Wade's scoring-title tape. Follow @HoopDynastyDaily.",
            "Which LeBron-Wade connection was the filthiest?",
        ],
    },
    39: {
        "summary": "Kobe Bryant's 2005-06 scoring season: 35.4 points per game, 62 through three quarters, the 81-point night and 27 games with 40-plus, carrying a thin Lakers roster back to the playoffs.",
        "caption": [
            "35.4 a night. 62 in three quarters. 81 in one game. 27 times over 40. The 05-06 Kobe season, dragging a thin Lakers roster into the playoffs.",
            "Most unstoppable scoring season ever?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#KobeBryant", "#NBAScoringChampion", "#81Points", "#Lakers", "#NBAHistory"],
        "pinned": [
            "Next: the 2005-06 top-ten reel and the 61 at MSG. Follow @HoopDynastyDaily.",
            "Which is wilder - 81 in a game or 35.4 over a season?",
        ],
    },
    40: {
        "summary": "A ball-movement compilation: extra passes, quick swings and unselfish cuts where nobody holds the ball and the best shot wins.",
        "caption": [
            "No ball hog, no problem. A clean reel of the extra pass, the quick swing and the guy who gives up a good shot for a great one.",
            "Which team moves the ball best right now?",
            "@HoopDynastyDaily",
        ],
        "hashtags": ["#TeamBasketball", "#BallMovement", "#NBA", "#BasketballIQ", "#Basketball"],
        "pinned": [
            "Next: the Nash assist tape and the alley-oop reel. Follow @HoopDynastyDaily.",
            "Most beautiful passing team you've watched?",
        ],
    },
}

# idx23/25 are alternate source files (_xWT128) of the same clip as idx22/24 and
# share the same output/work directory; the copy is identical by construction.
ALIAS = {23: 22, 25: 24}


def load(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default)


def save(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def backup(p):
    b = p + ".pre-copy-rewrite"
    if os.path.exists(p) and not os.path.exists(b):
        shutil.copy2(p, b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write files")
    ap.add_argument("--only", help="comma separated idx list")
    ap.add_argument("--export", action="store_true",
                    help="write diag/post-copy.json (data source for auto_decide.py) and exit")
    args = ap.parse_args()

    if args.export:
        data = {}
        for idx in sorted(set(COPY) | set(ALIAS)):
            v = COPY[ALIAS.get(idx, idx)]
            data[str(idx)] = {
                "summary": v["summary"],
                "caption": (NL + NL).join(v["caption"]),
                "alt_caption": v["caption"][0],
                "hashtags": list(v["hashtags"]),
                "pinned_comment": (NL + NL).join(v["pinned"]),
            }
        p = os.path.join(ROOT, "diag", "post-copy.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("exported %d per-video copies -> %s" % (len(data), p))
        return

    targets = sorted(set(COPY) | set(ALIAS))
    if args.only:
        want = {int(x) for x in args.only.split(",")}
        targets = [i for i in targets if i in want]

    seen_dirs = set()
    done, skipped = [], []
    for idx in targets:
        src_idx = ALIAS.get(idx, idx)
        v = COPY[src_idx]
        _, out, work = Q.paths(idx)
        md = os.path.join(out, "tiktok-post.md")
        if idx in EXCLUDED or idx in MANUAL or not os.path.exists(md):
            skipped.append(idx)
            continue
        if out in seen_dirs:
            skipped.append(idx)
            continue
        seen_dirs.add(out)

        assert len(v["hashtags"]) == 5, "idx%02d needs exactly 5 hashtags" % idx
        assert not any(t.lower() in ("#fyp", "#viral", "#foryou", "#trending")
                       for t in v["hashtags"]), "idx%02d banned generic tag" % idx
        assert "Erase-only" not in v["summary"], "idx%02d production note in summary" % idx

        caption = (NL + NL).join(v["caption"])
        pinned = (NL + NL).join(v["pinned"])

        print("idx%02d  %s" % (idx, os.path.basename(out)[:62]))
        if not args.apply:
            done.append(idx)
            continue

        pc_path = os.path.join(work, "post-content.json")
        vu_path = os.path.join(work, "video-understanding.json")
        backup(pc_path)
        backup(vu_path)
        pc = load(pc_path, {})
        pc.update({
            "summary": v["summary"],
            "caption": caption,
            "alt_caption": v["caption"][0],
            "hashtags": list(v["hashtags"]),
            "pinned_comment": pinned,
            "note": "2026-11-15 hand-authored per video (rewrite_posts.py); "
                    "facts from source filename + OCR/ASR, not the cover headline.",
        })
        save(pc_path, pc)

        vu = load(vu_path, {})
        vu["summary"] = v["summary"]
        vu["note"] = "2026-11-15 per-video summary (rewrite_posts.py)"
        save(vu_path, vu)

        r = subprocess.run(
            [PY, os.path.join(SK, "generate_post.py"),
             "--work-dir", work, "--output-dir", out,
             "--profile", "nba", "--post-content", pc_path],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("idx%02d generate_post failed: %s %s" %
                             (idx, r.stdout[-400:], r.stderr[-400:]))
        done.append(idx)

    print("\n%s: %d posts, skipped %d %s" %
          ("APPLIED" if args.apply else "DRY RUN", len(done), len(skipped), skipped))


if __name__ == "__main__":
    main()

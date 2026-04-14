"""Unit test the parsing + validation stages against realistic fixtures
that mirror Hockey Reference's actual markup (including tables hidden
inside HTML comments, the duplicate 'G' column, rank columns, and
Utah Mammoth's 2025-26 rename)."""

import sys
sys.path.insert(0, "/home/claude")

from nhl_pipeline import (
    parse_games, parse_team_stats, parse_team_analytics, validate
)

# --- Fixture: schedule page with a couple postponed games and a header repeat ---
GAMES_HTML = """
<html><body>
<table id="games">
  <thead><tr>
    <th>Date</th><th>Visitor</th><th>G</th>
    <th>Home</th><th>G</th><th>Att.</th><th>Notes</th>
  </tr></thead>
  <tbody>
    <tr><td>2025-10-07</td><td>Boston Bruins</td><td>3</td>
        <td>Toronto Maple Leafs</td><td>4</td><td>19100</td><td></td></tr>
    <tr><td>2025-10-08</td><td>Utah Mammoth</td><td>2</td>
        <td>Colorado Avalanche</td><td>5</td><td>18000</td><td></td></tr>
    <tr><td>2025-10-09</td><td>New York Rangers</td><td>1</td>
        <td>Montreal Canadiens</td><td>2</td><td>21105</td><td>SO</td></tr>
    <tr><td>Date</td><td>Visitor</td><td>G</td>
        <td>Home</td><td>G</td><td>Att.</td><td>Notes</td></tr>
    <tr><td>2025-10-10</td><td>Vegas Golden Knights</td><td></td>
        <td>Seattle Kraken</td><td></td><td></td><td>Postponed</td></tr>
    <tr><td>2025-10-07</td><td>Boston Bruins</td><td>3</td>
        <td>Toronto Maple Leafs</td><td>4</td><td>19100</td><td></td></tr>
  </tbody>
</table>
</body></html>
"""

# --- Fixture: season page w/ stats table live + analytics hidden in comment ---
SEASON_HTML = """
<html><body>
<table id="stats">
  <thead><tr>
    <th>Rk</th><th>Team</th><th>AvAge</th><th>GP</th><th>W</th><th>L</th>
    <th>OL</th><th>PTS</th><th>PTS%</th><th>GF</th><th>GA</th><th>SRS</th>
  </tr></thead>
  <tbody>
    <tr><td>1</td><td>Boston Bruins</td><td>28.5</td><td>10</td><td>7</td>
        <td>2</td><td>1</td><td>15</td><td>.750</td><td>35</td><td>22</td><td>1.20</td></tr>
    <tr><td>2</td><td>Toronto Maple Leafs*</td><td>27.9</td><td>10</td><td>6</td>
        <td>3</td><td>1</td><td>13</td><td>.650</td><td>33</td><td>28</td><td>0.50</td></tr>
    <tr><td>3</td><td>Utah Mammoth</td><td>26.1</td><td>10</td><td>5</td>
        <td>4</td><td>1</td><td>11</td><td>.550</td><td>28</td><td>27</td><td>0.10</td></tr>
    <tr><td>4</td><td>Colorado Avalanche</td><td>28.0</td><td>10</td><td>8</td>
        <td>2</td><td>0</td><td>16</td><td>.800</td><td>40</td><td>20</td><td>2.00</td></tr>
    <tr><td>5</td><td>New York Rangers</td><td>27.4</td><td>10</td><td>4</td>
        <td>5</td><td>1</td><td>9</td><td>.450</td><td>25</td><td>30</td><td>-0.50</td></tr>
    <tr><td>6</td><td>Montreal Canadiens</td><td>25.8</td><td>10</td><td>6</td>
        <td>3</td><td>1</td><td>13</td><td>.650</td><td>31</td><td>26</td><td>0.40</td></tr>
    <tr><td>7</td><td>Vegas Golden Knights</td><td>29.2</td><td>10</td><td>7</td>
        <td>3</td><td>0</td><td>14</td><td>.700</td><td>34</td><td>24</td><td>1.00</td></tr>
    <tr><td>8</td><td>Seattle Kraken</td><td>28.8</td><td>10</td><td>3</td>
        <td>6</td><td>1</td><td>7</td><td>.350</td><td>22</td><td>32</td><td>-1.00</td></tr>
    <tr><td></td><td>League Average</td><td>27.7</td><td>10</td><td>5.75</td>
        <td>3.5</td><td>0.75</td><td>12.25</td><td>.600</td><td>31</td><td>26</td><td>0.0</td></tr>
  </tbody>
</table>

<!--
<div>
<table id="stats_adv_5on5">
  <thead><tr>
    <th>Rk</th><th>Team</th><th>S%</th><th>SV%</th><th>PDO</th>
    <th>CF</th><th>CA</th><th>CF%</th><th>FF</th><th>FA</th><th>FF%</th>
    <th>xGF</th><th>xGA</th><th>aGF</th><th>aGA</th>
  </tr></thead>
  <tbody>
    <tr><td>1</td><td>Boston Bruins</td><td>9.1</td><td>.920</td><td>101.1</td>
        <td>520</td><td>480</td><td>52.0</td><td>400</td><td>380</td><td>51.3</td>
        <td>28.5</td><td>25.1</td><td>29</td><td>24</td></tr>
    <tr><td>2</td><td>Toronto Maple Leafs</td><td>8.7</td><td>.910</td><td>99.7</td>
        <td>500</td><td>500</td><td>50.0</td><td>390</td><td>395</td><td>49.7</td>
        <td>27.0</td><td>27.3</td><td>26</td><td>28</td></tr>
    <tr><td>3</td><td>Utah Mammoth</td><td>8.0</td><td>.905</td><td>98.5</td>
        <td>470</td><td>510</td><td>48.0</td><td>360</td><td>400</td><td>47.4</td>
        <td>24.0</td><td>27.5</td><td>22</td><td>28</td></tr>
    <tr><td>4</td><td>Colorado Avalanche</td><td>10.5</td><td>.925</td><td>103.0</td>
        <td>560</td><td>440</td><td>56.0</td><td>430</td><td>360</td><td>54.4</td>
        <td>32.0</td><td>22.5</td><td>34</td><td>21</td></tr>
    <tr><td>5</td><td>New York Rangers</td><td>7.9</td><td>.905</td><td>98.4</td>
        <td>460</td><td>520</td><td>47.0</td><td>350</td><td>410</td><td>46.0</td>
        <td>23.0</td><td>28.0</td><td>21</td><td>29</td></tr>
    <tr><td>6</td><td>Montreal Canadiens</td><td>9.3</td><td>.915</td><td>100.8</td>
        <td>505</td><td>495</td><td>50.5</td><td>395</td><td>392</td><td>50.2</td>
        <td>27.5</td><td>26.8</td><td>27</td><td>26</td></tr>
    <tr><td>7</td><td>Vegas Golden Knights</td><td>9.8</td><td>.918</td><td>101.6</td>
        <td>530</td><td>470</td><td>53.0</td><td>410</td><td>370</td><td>52.5</td>
        <td>29.0</td><td>24.0</td><td>30</td><td>23</td></tr>
    <tr><td>8</td><td>Seattle Kraken</td><td>7.2</td><td>.898</td><td>97.0</td>
        <td>450</td><td>530</td><td>46.0</td><td>340</td><td>420</td><td>44.7</td>
        <td>21.0</td><td>29.0</td><td>20</td><td>31</td></tr>
  </tbody>
</table>
</div>
-->
</body></html>
"""

print("=" * 72)
print("Testing parse_games...")
print("=" * 72)
games = parse_games(GAMES_HTML)
print(games.to_string())
print()
print("dtypes:", games.dtypes.to_dict())
print("shape:", games.shape)

print()
print("=" * 72)
print("Testing parse_team_stats...")
print("=" * 72)
teams = parse_team_stats(SEASON_HTML)
print(teams.to_string())
print()
print("columns:", teams.columns.tolist())
print("shape:", teams.shape)

print()
print("=" * 72)
print("Testing parse_team_analytics...")
print("=" * 72)
analytics = parse_team_analytics(SEASON_HTML, teams)
print(analytics.to_string())
print()
print("columns:", analytics.columns.tolist())
print("shape:", analytics.shape)

print()
print("=" * 72)
print("Running validation...")
print("=" * 72)
validate(games, teams, analytics)
print("OK")

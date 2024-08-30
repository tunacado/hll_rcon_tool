import {
  AppBar,
  Link,
  Grid,
  Toolbar,
  Typography,
  LinearProgress,
  GridList,
  GridListTile,
  GridListTileBar,
} from "@material-ui/core";
import React from "react";
import { get, handle_http_errors, showResponse } from "../../utils/fetchUtils";
import { List as iList, Map, fromJS } from "immutable";
import moment from "moment";
import useMediaQuery from "@material-ui/core/useMediaQuery";
import Scores from "./Scores";
import { Link as RouterLink } from "react-router-dom";

const LiveSessionScore = () => (
  <LiveScore
    endpoint="get_live_scoreboard"
    title="LIVE SESSIONS"
    explainText={
      <React.Fragment>
        Only ingame players are shown. Stats are reset on disconnection, not per
        game, check the{" "}
        <Link
          variant="button"
          color="inherit"
          component={RouterLink}
          to="/livegamescore"
        >
          Live Game
        </Link>{" "}
        or{" "}
        <Link
          variant="button"
          color="inherit"
          component={RouterLink}
          to="/gamescoreboard"
        >
          Past games
        </Link>{" "}
        for historical data. Real deaths only are counted (e.g. not redeploys /
        revives)
      </React.Fragment>
    }
  />
);

const LiveGameScore = () => (
  <LiveScore
    endpoint="get_live_game_stats"
    title="CURRENT GAME"
    explainText={
      <React.Fragment>
        All players that are or were in the game are shown, check the{" "}
        <Link
          variant="button"
          color="inherit"
          component={RouterLink}
          to="/livescore"
        >
          Live Sessions
        </Link>{" "}
        for live stats accross several games or{" "}
        <Link
          variant="button"
          color="inherit"
          component={RouterLink}
          to="/gamescoreboard"
        >
          Past games
        </Link>{" "}
        for historical data. Real deaths only are counted (e.g. not redeploys /
        revives)
      </React.Fragment>
    }
  />
);

const LiveScore = ({ endpoint, explainText, title }) => {
  const [stats, setStats] = React.useState(new iList());
  const [serverState, setServerState] = React.useState(new Map());
  const [isLoading, setIsLoading] = React.useState(true);
  const [isPaused, setPaused] = React.useState(false);
  const [refreshIntervalSec, setRefreshIntervalSec] = React.useState(10);
  const durationToHour = (val) =>
    new Date(val * 1000).toISOString().substr(11, 5);
  const scores = stats.get("stats", new iList());
  const lastRefresh = stats.get("snapshot_timestamp")
    ? moment.unix(stats.get("snapshot_timestamp")).format()
    : "N/A";

  const getData = () => {
    setIsLoading(true);
    get(endpoint)
      .then((res) => showResponse(res, endpoint, false))
      .then((data) => {
        const map_ = fromJS(data.result || new iList());
        setStats(map_);
        setRefreshIntervalSec(map_.get("refresh_interval_sec", 10));
        // TODO add code to sync the refresh time with one of the server by checking the last refresh timestamp
      })
      .catch(handle_http_errors);
    get("get_public_info")
      .then((res) => showResponse(res, "get_public_info", false))
      .then((data) => setServerState(fromJS(data.result)))
      .then(() => setIsLoading(false))
      .catch(handle_http_errors);
  };

  React.useEffect(getData, []);

  React.useEffect(() => {
    if (!isPaused) {
      const interval = setInterval(getData, refreshIntervalSec * 1000);
      return () => clearInterval(interval);
    }
  }, [isPaused, refreshIntervalSec]);

  document.title = serverState.get("name", new Map()).get("name", "HLL Stats");
  let started = serverState.get("current_map", new Map()).get("start");
  started = started
    ? new Date(Date.now() - new Date(started * 1000))
        .toISOString()
        .substr(11, 8)
    : "N/A";

  return (
    <>
      <Grid
        container
        spacing={2}
        justify="center"
      >
        <Grid
          item
          xs={12}
        >
          {process.env.REACT_APP_PUBLIC_BUILD ? (
            <Typography color="secondary" variant="h4">
              {serverState.get("name", new Map()).get("name")}
            </Typography>
          ) : (
            <Link
              href={`http://${window.location.hostname}:${serverState.get(
                "public_stats_port"
              )}`}
              target="_blank"
            >
              Public version on port {serverState.get("public_stats_port")} -
              https: {serverState.get("public_stats_port_https")}
            </Link>
          )}
        </Grid>

        {process.env.REACT_APP_PUBLIC_BUILD ? (
          <Grid
            xs={12}
            md={10}
            lg={10}
            xl={8}
          >
            <LiveHeader
              serverState={serverState}
              started={started}
              lastRefresh={lastRefresh}
              refreshIntervalSec={refreshIntervalSec}
              setPaused={setPaused}
              isPaused={isPaused}
              isLoading={isLoading}
              explainText={explainText}
              title={title}
            />
          </Grid>
        ) : (
          ""
        )}
      </Grid>
      <Grid
        container
        spacing={2}
        justify="center"
        
      >
        <Scores
          
          serverState={serverState}
          started={started}
          lastRefresh={lastRefresh}
          refreshIntervalSec={refreshIntervalSec}
          setPaused={setPaused}
          isPaused={isPaused}
          isLoading={isLoading}
          scores={scores}
          durationToHour={durationToHour}
          type="live"
        />
      </Grid>
    </>
  );
};

const LiveHeader = ({
  serverState,
  started,
  lastRefresh,
  refreshIntervalSec,
  setPaused,
  isPaused,
  isLoading,
  explainText,
  title,
}) => {
  const nextMapString = React.useMemo(() => {
    const [map, nbVotes] = serverState
      .get("vote_status")
      ?.get("winning_maps")
      ?.get(0) || ["", 0];
    const totalVotes = serverState.get("vote_status")?.get("total_votes");
    const nextMap = serverState.get("next_map")?.get("map")?.get("pretty_name");

    if (map === nextMap) {
      return `Nextmap ${nextMap} with ${nbVotes} out of ${totalVotes} votes`;
    }
    return `Nextmap: ${nextMap}`;
  }, [serverState]);

  return (
    <AppBar position="relative" style={{ minHeight: "144px" }}>
      <Toolbar>
        <GridList cols={1}>
          <GridListTile>
            <Grid container spacing={1}>
              <Grid item xs={12}>
                <Typography variant="h4" display="inline" color="inherit">
                  {title}
                </Typography>
              </Grid>
              <Grid item xs={12}>
                <Typography variant="body2">{explainText}</Typography>
              </Grid>
              <Grid item xs={12}>
                <Typography variant="caption">
                  Auto-refresh every {refreshIntervalSec} seconds:{" "}
                  <Link onClick={() => setPaused(!isPaused)} color="secondary">
                    {isPaused ? "click to unpause" : "click to pause"}
                  </Link>{" "}
                  - Last update: {lastRefresh}
                </Typography>
              </Grid>
              <Grid item xs={12}>
                <LinearProgress
                  style={{ visibility: isLoading ? "visible" : "hidden" }}
                  
                  color="secondary"
                />
              </Grid>
            </Grid>
          </GridListTile>
          <GridListTile>
            <img
              alt="Map"
              src={`maps/${serverState
                .get("current_map")
                ?.get("map")
                ?.get("image_name", "unknown.webp")}`}
            />
            <GridListTileBar
              title={serverState
                .get("current_map", new Map())
                .get("map", new Map())
                .get("pretty_name")}
              subtitle=""
              titlePosition="top"
            />
            <GridListTileBar
              title={`Elapsed: ${started} - Players: ${serverState.get(
                "player_count"
              )}/${serverState.get("max_player_count")}`}
              subtitle={nextMapString}
              titlePosition="bottom"
            />
          </GridListTile>
        </GridList>
      </Toolbar>
    </AppBar>
  );
};

export { LiveGameScore, LiveSessionScore };

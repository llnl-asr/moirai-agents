# Lessons: pipeline_control

- [L-5bc710bd] When the user does not know the test command ("figure that out based on the repo"), clone the repo into workspaces/<app>-peek/src and read README, nextflow.config, conf/test.config and assets. lc.llnl.gov https clone asks for credentials and fails in the agent; the SSH form that worked is ssh://git@czgitlab.llnl.gov:7999/<group>/<repo>.git (lc.llnl.gov:7999 refuses). pipeline_new defaults ref to main: a repo whose branch is master gives session_error "no session for <url>" and leaves a session-less pipeline; call pipeline_new again with ref='master' (a second pipeline id is created, say which one is live).

- [L-a054e6a3] Blank draft chat with a git URL but no test command: pipeline_status without a pipeline argument returns every pipeline (59 KB, saved to a file), so grep the file for the draft id and read only its head. Then ask_user for the missing small test command before pipeline_new(kind='run').

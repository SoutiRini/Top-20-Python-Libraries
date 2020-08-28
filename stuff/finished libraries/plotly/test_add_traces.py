import sys
from unittest import TestCase

import plotly.graph_objs as go

if sys.version_info >= (3, 3):
    from unittest.mock import MagicMock
else:
    from mock import MagicMock


class TestAddTracesMessage(TestCase):
    def setUp(self):
        # Construct initial scatter object
        self.figure = go.Figure(
            data=[
                go.Scatter(y=[3, 2, 1], marker={"color": "green"}),
                go.Bar(y=[3, 2, 1, 0, -1], marker={"opacity": 0.5}),
            ],
            layout={"xaxis": {"range": [-1, 4]}},
            frames=[go.Frame(layout={"yaxis": {"title": "f1"}})],
        )

        # Mock out the message method
        self.figure._send_addTraces_msg = MagicMock()

    def test_add_trace(self):
        # Add a trace
        self.figure.add_trace(go.Sankey(arrangement="snap"))

        # Check access properties
        self.assertEqual(self.figure.data[-1].type, "sankey")
        self.assertEqual(self.figure.data[-1].arrangement, "snap")

        # Check message
        self.figure._send_addTraces_msg.assert_called_once_with(
            [{"type": "sankey", "arrangement": "snap"}]
        )

    def test_add_traces(self):

        # Add two traces
        self.figure.add_traces(
            [
                go.Sankey(arrangement="snap"),
                go.Histogram2dContour(line={"color": "cyan"}),
            ]
        )

        # Check access properties
        self.assertEqual(self.figure.data[-2].type, "sankey")
        self.assertEqual(self.figure.data[-2].arrangement, "snap")

        self.assertEqual(self.figure.data[-1].type, "histogram2dcontour")
        self.assertEqual(self.figure.data[-1].line.color, "cyan")

        # Check message
        new_uid1 = self.figure.data[-2].uid
        new_uid2 = self.figure.data[-1].uid
        self.figure._send_addTraces_msg.assert_called_once_with(
            [
                {"type": "sankey", "arrangement": "snap"},
                {"type": "histogram2dcontour", "line": {"color": "cyan"}},
            ]
        )


class TestAddTracesRowsColsDataTypes(TestCase):
    def test_add_traces_with_iterable(self):
        import plotly.express as px

        df = px.data.tips()
        fig = px.scatter(df, x="total_bill", y="tip", color="day")
        from plotly.subplots import make_subplots

        fig2 = make_subplots(1, 2)
        fig2.add_traces(fig.data, rows=[1,] * len(fig.data), cols=[1,] * len(fig.data))

        expected_data_length = 4

        self.assertEqual(expected_data_length, len(fig2.data))

    def test_add_traces_with_integers(self):
        import plotly.express as px

        df = px.data.tips()
        fig = px.scatter(df, x="total_bill", y="tip", color="day")
        from plotly.subplots import make_subplots

        fig2 = make_subplots(1, 2)
        fig2.add_traces(fig.data, rows=1, cols=2)

        expected_data_length = 4

        self.assertEqual(expected_data_length, len(fig2.data))

import torch as tc
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from collections import OrderedDict

import wargs
from utils import *
from gru import GRU

class NMT(nn.Module):

    def __init__(self, src_vocab_size, trg_vocab_size):

        super(NMT, self).__init__()

        self.encoder = Encoder(src_vocab_size, wargs.src_wemb_size, wargs.enc_hid_size)
        self.s_init = nn.Linear(wargs.enc_hid_size, wargs.dec_hid_size)
        self.tanh = nn.Tanh()
        self.ha = nn.Linear(wargs.enc_hid_size, wargs.align_size)

        self.decoder = Decoder(trg_vocab_size)

    def init_state(self, xs_h, xs_mask=None):

        assert xs_h.dim() == 3  # slen, batch_size, enc_size
        if xs_mask is not None:
            xs_h = (xs_h * xs_mask[:, :, None]).sum(0) / xs_mask.sum(0)[:, None]
        else:
            xs_h = xs_h.mean(0)

        return self.tanh(self.s_init(xs_h))

    def init(self, xs, xs_mask=None, test=True):

        if test:  # for decoding
            if wargs.gpu_id and not xs.is_cuda: xs = xs.cuda()
            xs = Variable(xs, requires_grad=False, volatile=True)

        xs = self.encoder(xs, xs_mask)
        s0 = self.init_state(xs, xs_mask)
        uh = self.ha(xs)

        return s0, xs, uh

    def forward(self, srcs, trgs, srcs_m, trgs_m):
        # (max_slen_batch, batch_size, enc_hid_size)
        s0, srcs, uh = self.init(srcs, srcs_m, False)

        return self.decoder(s0, srcs, uh, trgs, srcs_m, trgs_m)

class RelationLayer(nn.Module):

    def __init__(self, input_size, output_size, out_channels):

        super(RelationLayer, self).__init__()

        self.C_in = 1
        self.C_out = out_channels
        kernels = [1, 3, 5, 7]

        #self.convs = nn.ModuleList([nn.Conv2d(
        #    self.C_in, self.C_out, (k, input_size), padding=((k-1)/2,input_size)) for k in kernels])
        self.convs = nn.ModuleList([nn.Conv2d(self.C_in, self.C_out, (k, input_size),
                                              padding=((k-1)/2, 0)) for k in kernels])

        self.leakyRelu = nn.LeakyReLU(0.1)
        self.bn = nn.BatchNorm1d(self.C_out)

        length = len(kernels)

        self.mlpdec_in = length * self.C_out

        self.mlp = nn.Sequential(
            nn.Linear(2 * self.mlpdec_in, self.C_out),
            #nn.ReLU(),
            nn.LeakyReLU(0.1),
            nn.Linear(self.C_out, self.C_out),
            #nn.ReLU(),
            nn.LeakyReLU(0.1),
            nn.Linear(self.C_out, self.C_out),
            #nn.ReLU()
            nn.LeakyReLU(0.1),
            nn.Linear(self.C_out, self.C_out),
            #nn.ReLU()
            nn.LeakyReLU(0.1)
        )

        self.mlp_layer = nn.Sequential(
            nn.Linear(self.C_out, self.C_out),
            #nn.ReLU(),
            nn.LeakyReLU(0.1),
            nn.Linear(self.C_out, output_size),
            #nn.ReLU()
            nn.LeakyReLU(0.1)
        )

    def forward(self, x, xs_mask=None):

        L, B, E = x.size()
        x = x.permute(1, 0, 2)    # (B, L, E)

        x = x[:, None, :, :].expand((B, self.C_in, L, E))
        # (B, 1, L, E)

        x = [self.leakyRelu(conv(x)).squeeze(3) for conv in self.convs]
        # (B, out_channels, L, 1) -> (B, out_channels, L)

        #x = [F.max_pool1d(i, i.size(2)).squeeze(2) for i in x]
        x = tc.cat(x, dim=1)
        x = x.permute(2, 0, 1)
        #(L, B, out_channels * len(kernels))

        #if xs_mask is not None: xs_h = xs_h * xs_mask[:, :, None]
        x = x[None, :, :, :].expand(L, L, B, self.mlpdec_in)
        x = tc.cat([x, x.transpose(0, 1)], dim=-1)

        x = self.mlp(x).sum(0)
        #if xs_mask is not None: xs_h = xs_h * xs_mask[:, :, None]

        return self.mlp_layer(x)

class Encoder(nn.Module):

    '''
        Bi-directional Gated Recurrent Unit network encoder
    '''

    def __init__(self,
                 src_vocab_size,
                 input_size,
                 output_size,
                 with_ln=False,
                 prefix='Encoder', **kwargs):

        super(Encoder, self).__init__()

        self.output_size = output_size
        f = lambda name: str_cat(prefix, name)  # return 'Encoder_' + parameters name

        self.src_lookup_table = nn.Embedding(src_vocab_size,
                                             wargs.src_wemb_size, padding_idx=PAD)

        #self.relation_layer0 = RelationLayer(wargs.enc_hid_size, wargs.enc_hid_size, 80)
        #self.laynorm0 = LayerNormalization(wargs.enc_hid_size)

        self.relation_layer1 = RelationLayer(wargs.enc_hid_size, wargs.enc_hid_size, 80)
        #self.laynorm1 = LayerNormalization(wargs.enc_hid_size)

        #self.dropout = nn.Dropout(0.1)

        self.forw_gru = GRU(input_size, output_size, with_ln=with_ln, prefix=f('Forw'))
        self.back_gru = GRU(output_size, output_size, with_ln=with_ln, prefix=f('Back'))

        self.down0 = nn.Linear(2 * wargs.enc_hid_size, wargs.enc_hid_size)
        #self.down1 = nn.Linear(3 * wargs.enc_hid_size, wargs.enc_hid_size)
        self.down2 = nn.Linear(3 * wargs.enc_hid_size, wargs.enc_hid_size)
        self.down3 = nn.Linear(4 * wargs.enc_hid_size, wargs.enc_hid_size)

        #self.relation_layer2 = RelationLayer(wargs.enc_hid_size, wargs.enc_hid_size, 80)
        #self.relation_layer3 = RelationLayer(wargs.enc_hid_size, wargs.enc_hid_size, 80)
        #self.relation_layer4 = RelationLayer(wargs.enc_hid_size, wargs.enc_hid_size, 80)
        #self.down4 = nn.Linear(6 * wargs.enc_hid_size, wargs.enc_hid_size)
        #self.down5 = nn.Linear(7 * wargs.enc_hid_size, wargs.enc_hid_size)
        #self.down6 = nn.Linear(8 * wargs.enc_hid_size, wargs.enc_hid_size)

    def forward(self, xs, xs_mask=None, h0=None):

        max_L, b_size = xs.size(0), xs.size(1)
        xs_e = xs if xs.dim() == 3 else self.src_lookup_table(xs)

        right = []
        h = h0 if h0 else Variable(tc.zeros(b_size, self.output_size), requires_grad=False)
        if wargs.gpu_id: h = h.cuda()
        for k in range(max_L):
            # (batch_size, src_wemb_size)
            h = self.forw_gru(xs_e[k], xs_mask[k] if xs_mask is not None else None, h)
            right.append(h)

        out_1 = tc.stack(right, dim=0)
        out_1 = out_1 + xs_e

        in_2 = tc.cat([xs_e, out_1], dim=-1)
        in_3 = self.down0(in_2)

        #out_2 = self.relation_layer0(in_2, xs_mask)
        #out_2 = out_2 + in_2

        #in_3 = tc.cat([xs_e, out_1, out_2], dim=-1)
        #in_3 = self.down1(in_3)

        left = []
        h = h0 if h0 else Variable(tc.zeros(b_size, self.output_size), requires_grad=False)
        if wargs.gpu_id: h = h.cuda()
        for k in reversed(range(max_L)):
            h = self.back_gru(in_3[k], xs_mask[k] if xs_mask is not None else None, h)
            left.append(h)

        out_3 = tc.stack(left[::-1], dim=0)
        out_3 = out_3 + in_3

        #in_4 = tc.cat([xs_e, out_1, out_2, out_3], dim=-1)
        in_4 = tc.cat([xs_e, out_1, out_3], dim=-1)
        in_4 = self.down2(in_4)

        out_4 = self.relation_layer1(in_4, xs_mask)
        out_4 = out_4 + in_4

        #in_5 = tc.cat([xs_e, out_1, out_2, out_3, out_4], dim=-1)
        in_5 = tc.cat([xs_e, out_1, out_3, out_4], dim=-1)
        in_5 = self.down3(in_5)

        '''
        out_5 = self.relation_layer2(in_5, xs_mask)
        out_5 = out_5 + in_5

        in_6 = tc.cat([xs_e, out_1, out_2, out_3, out_4, out_5], dim=-1)
        in_6 = self.down4(in_6)

        out_6 = self.relation_layer3(in_6, xs_mask)
        out_6 = out_6 + in_6

        in_7 = tc.cat([xs_e, out_1, out_2, out_3, out_4, out_5, out_6], dim=-1)
        in_7 = self.down5(in_7)

        out_7 = self.relation_layer4(in_7, xs_mask)
        out_7 = out_7 + in_7

        in_8 = tc.cat([xs_e, out_1, out_2, out_3, out_4, out_5, out_6, out_7], dim=-1)
        in_8 = self.down6(in_8)
        '''

        return in_5

class Attention(nn.Module):

    def __init__(self, dec_hid_size, align_size):

        super(Attention, self).__init__()

        self.align_size = align_size
        self.sa = nn.Linear(dec_hid_size, self.align_size)
        self.tanh = nn.Tanh()
        self.sigmoid = nn.Sigmoid()
        self.a1 = nn.Linear(self.align_size, 1)

    def forward(self, s_tm1, xs_h, uh, xs_mask=None):

        #d1, d2, d3 = uh.size()
        # (b, dec_hid_size) -> (b, aln) -> (1, b, aln) -> (slen, b, aln) -> (slen, b)
        e_ij = self.a1(self.tanh(self.sa(s_tm1)[None, :, :] + uh)).squeeze(2).exp()
        if xs_mask is not None: e_ij = e_ij * xs_mask

        # probability in each column: (slen, b)
        e_ij = e_ij / e_ij.sum(0)[None, :]

        # weighted sum of the h_j: (b, enc_hid_size)
        attend = (e_ij[:, :, None] * xs_h).sum(0)

        return e_ij, attend

class Decoder(nn.Module):

    def __init__(self, trg_vocab_size, max_out=True):

        super(Decoder, self).__init__()

        self.max_out = max_out
        self.attention = Attention(wargs.dec_hid_size, wargs.align_size)
        self.trg_lookup_table = nn.Embedding(trg_vocab_size,
                                             wargs.trg_wemb_size, padding_idx=PAD)
        self.tanh = nn.Tanh()
        self.gru1 = GRU(wargs.trg_wemb_size, wargs.dec_hid_size)
        #self.gru2 = GRU(wargs.enc_hid_size, wargs.dec_hid_size, with_two_attents=True)
        self.gru2 = GRU(wargs.enc_hid_size, wargs.dec_hid_size)

        out_size = 2 * wargs.out_size if max_out else wargs.out_size
        self.ls = nn.Linear(wargs.dec_hid_size, out_size)
        self.ly = nn.Linear(wargs.trg_wemb_size, out_size)
        self.lc = nn.Linear(wargs.enc_hid_size, out_size)
        #self.lc2 = nn.Linear(wargs.enc_hid_size, out_size)

    def step(self, s_tm1, xs_h, uh, y_tm1, xs_mask=None, y_mask=None):

        if not isinstance(y_tm1, tc.autograd.variable.Variable):
            if isinstance(y_tm1, int): y_tm1 = tc.Tensor([y_tm1]).long()
            elif isinstance(y_tm1, list): y_tm1 = tc.Tensor(y_tm1).long()
            if wargs.gpu_id: y_tm1 = y_tm1.cuda()
            y_tm1 = Variable(y_tm1, requires_grad=False, volatile=True)
            y_tm1 = self.trg_lookup_table(y_tm1)

        s_above = self.gru1(y_tm1, y_mask, s_tm1)
        # (slen, batch_size) (batch_size, enc_hid_size)
        alpha_ij, attend = self.attention(s_above, xs_h, uh, xs_mask)
        #alpha_ij2, attend2 = self.attention(s_above, xs_rel_h, uh_rel, xs_mask)
        #s_t = self.gru2(attend, y_mask, s_above, x2_t=attend2)
        s_t = self.gru2(attend, y_mask, s_above)

        return attend, s_t, y_tm1, alpha_ij

    def forward(self, s_tm1, xs_h, uh, ys, xs_mask=None, ys_mask=None):

        #tlen_batch_s, tlen_batch_c, tlen_batch_c2 = [], [], []
        tlen_batch_s, tlen_batch_c = [], []
        y_Lm1, b_size = ys.size(0), ys.size(1)
        # (max_tlen_batch - 1, batch_size, trg_wemb_size)
        ys_e = ys if ys.dim() == 3 else self.trg_lookup_table(ys)
        for k in range(y_Lm1):
            attend, s_tm1, _, _ = self.step(s_tm1, xs_h, uh, ys_e[k],
                                            xs_mask if xs_mask is not None else None,
                                            ys_mask[k] if ys_mask is not None else None)
            tlen_batch_c.append(attend)
            #tlen_batch_c2.append(attend2)
            tlen_batch_s.append(s_tm1)

        s = tc.stack(tlen_batch_s, dim=0)
        c = tc.stack(tlen_batch_c, dim=0)
        #c2 = tc.stack(tlen_batch_c2, dim=0)
        del tlen_batch_s, tlen_batch_c

        logit = self.step_out(s, ys_e, c)
        if ys_mask is not None: logit = logit * ys_mask[:, :, None]  # !!!!
        del s, c

        return logit

    def step_out(self, s, y, c):

        # (max_tlen_batch - 1, batch_size, dec_hid_size)
        #logit = self.ls(s) + self.ly(y) + self.lc(c) + self.lc2(c2)
        logit = self.ls(s) + self.ly(y) + self.lc(c)
        # (max_tlen_batch - 1, batch_size, out_size)

        if logit.dim() == 2:    # for decoding
            logit = logit.view(logit.size(0), logit.size(1)/2, 2)
        elif logit.dim() == 3:
            logit = logit.view(logit.size(0), logit.size(1), logit.size(2)/2, 2)

        return logit.max(-1)[0] if self.max_out else self.tanh(logit)

